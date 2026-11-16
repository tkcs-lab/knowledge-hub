# Knowledge hub runbook

For the operator. Names of variables and keys are in [configuration](configuration.md); the reasons behind the rules are in [design](design.md). Commands run as the `kh` user inside the container, with the `KH_*` environment of the service (see `deploy/compose.example.yaml`). Secrets are files, never variables, and nothing in this document is a real secret or host.

Not run in the cloud, so yours to do and check by hand: the backup script's `pg_dump` against the protected tables (`deploy/backup-kh.sh`). Check it on the first deploy. The Dockerfile build runs in the `ci` workflow on every pull request.

## Install order

1. **Roles and database.** As the Postgres superuser create the database `kh` with the `vector` extension and the four roles: `kh_migrator`, `kh_app` and `kh_indexer` (LOGIN, NOBYPASSRLS, one password each) and `kh_provenance` (NOLOGIN). Also run `GRANT kh_provenance TO kh_migrator`. No role is a superuser and none has BYPASSRLS. The migrations only `GRANT`; they never create roles.
2. **Secrets.** Put one file per secret under the secrets directory and name it in the matching `*_FILE` variable: the three database passwords, `kh_hub_bot_token`, `kh_indexer_token`, `kh_webhook_hmac`, the S3 access and secret key, `kh_cursor_key` and, if you want metrics, `kh_metrics_token`. Write `kh.yaml` (start from `deploy/kh.example.yaml`). Leave `trust_anchor: null` for every T0 repo for now: the anchors come after the seed pull requests are merged (see "Bootstrapping the trust anchor").
3. **Migrate.** `kh migrate`. A changed, already applied migration file refuses the run.
4. **Serve.** `kh serve` (the image does this). The worker starts only when `KH_FORGEJO_URL` is set. `/healthz` answers `ok`.
5. **Ollama.** Start the `ollama` service of `deploy/compose.example.yaml` (pinned image, memory limit 3 GB, one model loaded, kept loaded). The `kh` network has no internet access, so `ollama pull` cannot run there: pull the model once with a short-lived container on the default bridge network, which has egress, writing to the same models volume `kh-ollama-models`. Use the image tag of the compose file:

   ```bash
   docker run --rm -v kh-ollama-models:/root/.ollama --entrypoint sh ollama/ollama:0.40.16 \
     -c 'ollama serve >/dev/null 2>&1 & until ollama list >/dev/null 2>&1; do sleep 1; done; ollama pull bge-m3'
   ```

   Check with `docker compose exec ollama ollama list`. Ollama's documentation shows no one-off pull; this is the usual way and is confirmed on the first deploy. Until the model is available, embedding fails; run `kh rebuild` and `kh requeue-embeddings` afterwards. Embedding runs on the CPU and no speed is measured yet. To measure it on the host: `docker compose exec -T knowledge-hub python - --count 200 < scripts/bench_embed.py` prints chunks per second; a `kh rebuild` takes about the number of chunks (`SELECT count(*) FROM kh.chunks`) divided by that. Watch `docker stats` during the first rebuild to confirm the 3 GB limit.
6. **Index and record the config.** Run `kh rebuild`, then `kh recompute-visibility` **once**. The audience-map loader does not recompute visibility when no `__config__` hash is stored, so until you run it `kh check` reports config drift. Then run `kh check` and expect exit 0.
7. **Webhook.** In Forgejo set an organisation webhook to `http://knowledge-hub:8080/webhooks/forgejo` with the secret from `kh_webhook_hmac`, for push and pull request events.
8. **PrivacyFence.** Declare the hub as an upstream and map groups to audiences (section "PrivacyFence setup"), restart PrivacyFence, then create the n8n token (PrivacyFence follow-ups). The declaration must carry `email_for_visibility: true`: without it no caller sees attendee-only records.

## Bootstrapping the trust anchor

A fresh Forgejo has no reviewed history, so a T0 repo has no real `trust_anchor` yet. Until one is set, every commit including the root must be an approved pull request merge, so the seed content is indexed as `disputed`. That is expected on a fresh host; the alert is `file <path> on <repo>/main has no approved merge PR`.

1. For each T0 repo put the seed documents on a branch and open a pull request. Someone other than its author approves it in Forgejo and merges it into `main` with a merge commit.
2. Read the anchor from Forgejo: the merge commit of that first pull request, the oldest merge commit in the commit list of `main` (or in a clone: `git log --first-parent --merges --reverse --format=%H main | head -1`). It is the first reviewed commit on `main`. An initial commit made when the repo was created sits before it and is covered by the anchor.
3. Write that 40-character sha as `trust_anchor` of the repo in `kh.yaml`, deploy the file and run `kh rebuild --repo <repo>`, `kh recompute-visibility` and `kh check` (exit 0). The seed documents are now normative; later changes still need approved pull requests.

Setting an anchor trusts everything up to and including that commit: read the commit first.

## PrivacyFence setup

The declaration must carry `email_for_visibility: true`: without it no caller sees attendee-only records.

Copy `deploy/privacyfence-upstream.example.yaml` to the PrivacyFence host, set `PRIVACYFENCE_UPSTREAM_MCP_FILE` to it and restart PrivacyFence; the file and the hub's tool list are read at startup only. Its `audience` equals `KH_PF_AUDIENCE`. Then map Workspace groups to the hub's audiences in PrivacyFence's `admin_config.json` (re-read when it changes; edit it while nobody is saving on the Admin page):

```json
"upstream_mcp": {
  "kh": {"groups": {
    "everyone@company.example": ["all"],
    "mgmt@company.example": ["mgmt"],
    "hr@company.example": ["hr"],
    "contracts@company.example": ["contracts"]
  }}
}
```

Audience names must exist in `audiences` of `kh.yaml`. Only `all` is added at once; the others wait for an administrator's approval in PrivacyFence (Admin, Group membership).

## Routine tasks

| Task | Command and notes |
|---|---|
| Re-index after review | `kh rebuild` (all repos) or `kh rebuild --repo canonical-all`. Idempotent: a second run changes nothing. |
| Look for drift | `kh check`. It compares HEAD and content hashes with the index, runs the chunk drift query and compares the config hash. Exit 1 means drift; read the printed lines. |
| Daily job | `kh daily`, once a day from a scheduler. It marks documents stale when `review_by` has passed, syncs the Grist tables into the entity cache, deletes cache rows no longer in Grist and purges expired claim ids. It sets entity visibility from `config.entities` on every run, so an `entities` change takes effect at the next run. It exits 2 unless all three `KH_GRIST_*` settings are set and 1 on a Grist error. |
| After editing the audience map or repo audiences | `kh recompute-visibility`. Run it after any change to `audiences`, `repos` or `raw_audience_map` in `kh.yaml`, once the new file is deployed. It rewrites visibility of raw rows, documents, derived records, candidates and conflicts in one transaction. |
| Queue missing embeddings | `kh requeue-embeddings` queues jobs for raw and derived records that have no chunks. It prints the number queued. |
| Change the embedding model or chunker | Change `KH_EMBED_MODEL` or `KH_CHUNKER_VERSION`, restart, run `kh rebuild`, then `kh requeue-embeddings` for T2 and T3. Vectors of another model are never compared. |

Notes:

- A rebuild after a rewritten `main` clears the stored indexed sha for the repo first, so the walk starts from the trust anchor and then indexes the new head.
- The rate limiter (`KH_RATE_LIMIT_PER_MIN`) is per process: with several replicas each one counts alone.
- `/metrics` exists only when `KH_METRICS_TOKEN_FILE` is set. It needs `Authorization: Bearer <token>` and reads the token file on every request.

## Merge verdicts and `--rejudge`

Each merge commit after the trust anchor is judged once, and the verdict (trusted or disputed) is stored in `kh.merge_verdicts`. Every later sync and rebuild reuses it; only `kh rebuild --repo R --rejudge` (or `kh rebuild --rejudge` for all repos) deletes and judges again. A merge whose pull request Forgejo does not yet report as merged is not stored and is judged again on the next sync.

Consequences:

- A person leaving a team does not turn their earlier self-merges disputed.
- Enabling `self_merge_teams` does not bless history that was already judged; `--rejudge` does that on purpose.
- A disputed merge stays disputed until `--rejudge`, even if an approval is added afterwards.
- Older version rows that were disputed stay disputed after a rejudge even when the merge is now trusted, because the original per-file status is not stored; only the current document row changes.
- Changing `bot_logins` or `self_merge_teams`, an account compromise, or a reviewer later found unfit all need `kh rebuild --repo R --rejudge`.
- The first sync after upgrading to 0.2.0 judges the whole history once. Run `kh rebuild` right after `kh migrate`.

Use `--rejudge` after an account compromise, after changing `bot_logins` or `self_merge_teams`, after a team was cleaned up when you want history judged by today's membership, and consciously after a restore (see below).

`kh.merge_verdicts` is not in the backup script: it is derived from git like the other index tables. After a restore, `kh rebuild` judges the history again with today's team membership; run `kh rebuild --rejudge` knowingly.

Behaviour changes:

- A review dismissed after the merge no longer disputes the document: verdicts are fixed at first judgement.
- `kh rebuild` no longer re-judges; only `--rejudge` does.
- Team membership is read when a merge is first judged, not when it was merged.
- The indexer token needs `read:organization` when `self_merge_teams` is set, and the indexer user may need to be a member of the organisation for team reads.
- Upgrade the CI runner image before adding `self_merge_teams` or `bot_logins` to `kh.yaml`: older images refuse unknown keys.
- The walk accepts only real merge commits of the PR head. Allow only the plain merge style on canonical repositories; squash, rebase and fast-forward merges are non-merge commits and untrust the repository as before.
- `kh-admins` in `self_merge_teams` lets admins self-merge `owners.yml`. That is the owner's decision.

A mistyped team in `self_merge_teams` shows up as a failing sync: a repository with a self-merge-shaped merge is retried with `team X does not exist in Forgejo` in `index_jobs.last_error`. Fix the name in `kh.yaml` and restart.

## Alerts

`index_alerts` is readable only by the indexer role. Read it as the Postgres superuser, or as `kh_indexer`:

```sql
SELECT created_at, repo, path, message FROM kh.index_alerts ORDER BY created_at DESC LIMIT 50;
```

Alerts are de-duplicated on repo, path and message. After you fix the cause, re-index with `kh rebuild --repo <repo>` and check that no new alert appears.

| Alert text | What it means and what to do |
|---|---|
| `file <path> on <repo>/main has no approved merge PR` | A file was last changed by a commit that was not an approved pull request merge. The document is indexed as `disputed` and is not a normative answer. Find the commit in Forgejo. If the change is good, open a pull request that touches the file, have someone other than the author approve it and merge. If it is not, revert it by a pull request. Then `kh rebuild --repo <repo>`. |
| `trust anchor <sha> is not on main of <repo>` | The configured `trust_anchor` is not on the first-parent chain of `main`, so the whole repo is untrusted and every document is `disputed`. Check the sha in `kh.yaml` for a typo. If `main` was rewritten, review the history first, then set the anchor to a commit you have verified, deploy the config, run `kh rebuild --repo <repo>` and `kh recompute-visibility`. |
| `non-merge commit <sha> on main of <repo>` | Someone pushed straight to `main`. The repo is marked untrusted and its documents are `disputed` until you review. Turn on branch protection in Forgejo. Review the commit. When the history is acceptable, move `trust_anchor` in `kh.yaml` to the newest verified commit, deploy and run `kh rebuild --repo <repo>`. |
| `frontmatter must not set <key>; the server decides tier and audience` | A file set `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by`, `approved_at`, `stamp`, `generated_by` or `derived_from`. The file is not indexed and an earlier row of it is removed. Remove the key by a pull request. Other `frontmatter: ...` alerts (missing key, unknown domain, bad `review_by`, `status` not `current` or `superseded`) are fixed the same way. |
| `main of <repo> was rewritten; run kh rebuild after review` | The new head of `main` does not continue from the last indexed sha. The index is unchanged. Inspect the force push in Forgejo. If it was intended, run `kh rebuild --repo <repo>`. If not, restore the branch from a clone. |
| `duplicate id <id> in <repo>: <paths>` | Two files in one repo carry the same frontmatter `id`. Files without a single approved merge are rejected. Give one of them a new id. |
| `supersedes/superseded_by must name a document in the same repo` | A reference points outside the repo. The file is `disputed`. Replace the reference or move the document. |
| `pull request <repo>#<n> does not match candidate <id>` | A `bot/promote/<id>` pull request event named a different repo or number than the candidate row. Nothing was changed. Check the pull request in Forgejo and close it if it is not the bot's. |

### Governance checks

The `kh validate` gate in each canonical repo's CI mirrors three of these alerts, so a pull request fails before they can appear: `frontmatter must not set <key>; the server decides tier and audience`, `duplicate id <id> in <repo>: <paths>` and `supersedes/superseded_by must name a document in the same repo`. If one shows up anyway, the file reached `main` without the gate (a repo seeded without the workflow, or the status was not required). Look at the `kh/validate` and `kh/domain-owners` statuses of the merged pull request; setup, tokens and triggers are in [governance](governance.md).

## Restore of T2 and T3

Documents, chunks and the entity cache are rebuilt from Forgejo and Grist, so only T2, T3, promotion candidates and conflicts are restored from `deploy/backup-kh.sh`. The script writes `kh-t2t3-<date>.dump` (custom format, INSERT statements) and mirrors the blob bucket into `<BACKUP_DIR>/blobs/`. Back both up off the box (restic).

1. Start with an empty database: create the roles (install step 1) and run `kh migrate`.
2. As the Postgres superuser restore the data only, with triggers off, so the visibility the backup holds is kept as it was and no embedding jobs fire:

   ```bash
   pg_restore --data-only --disable-triggers --no-owner -d kh kh-t2t3-<date>.dump
   ```

3. Copy the blobs back: `aws s3 sync "<BACKUP_DIR>/blobs/" s3://<bucket> --endpoint-url <endpoint>`.
4. Run `kh rebuild` for the git-derived tables.
5. Run `kh recompute-visibility` once. A restore leaves no `__config__` hash, so the loader does not recompute by itself and `kh check` would report drift.
6. Run `kh requeue-embeddings`. Chunks are not in the dump, and restoring with triggers off queued nothing, so this is required after every restore.
7. Run `kh check` and expect exit 0, then ask an agent for a `search` that hits a restored record.

Garage keeps one version of each object, so the mirror is only as old as the last run; history comes from restic.

## Rotating secrets

**Webhook secret.** Write a new value to the `kh_webhook_hmac` file, set the same value as the secret of the Forgejo organisation webhook, with no restart (the file is read on every request). Between the two changes pushes answer 401 and are not indexed, so finish with `kh rebuild`. Trigger a test delivery from Forgejo and expect 202.

**Forgejo tokens.** Create the new token in Forgejo under the same user (`kh-hub-bot` or the indexer user) with the same scopes, write it to `kh_hub_bot_token` or `kh_indexer_token`, restart, then revoke the old token. The indexer token is read only. The bot token may write only to `bot/promote/*` branches and open pull requests. Check with `kh check` and a harmless `kh rebuild --repo <repo>`.

**Metrics token.** Replace the content of the `KH_METRICS_TOKEN_FILE` file; it is re-read per request, no restart.

**Database passwords.** Change the role password in Postgres and the matching file, then restart.

## PrivacyFence follow-ups

- **Create the n8n token.** Scoped tokens can call gated upstream tools through an administrator's tick (PrivacyFence ADR 0300, unreleased at the time of writing: the form shows the group "Upstream tools that change data or release results unseen" only in a PrivacyFence version that has it). An administrator opens PrivacyFence, Settings, Scoped tokens, New token, and:
  1. names the token for the workflow and sets an expiry;
  2. ticks `kh_ingest_raw` and `kh_write_derived` under "Upstream tools that change data or release results unseen", and `kh_propose_change` only if the workflow opens promotion pull requests; the read tools the workflow needs (`kh_search`, ...) are ticked as usual;
  3. under "Audiences for Knowledge hub" ticks exactly the audiences that `raw_audience_map` gives the series and folders the workflow ingests (the token must hold every mapped audience, or `ingest_raw` is refused), and no others; a token never holds more than its creator, and one with none ticked sees no audience;
  4. stores the secret in n8n's credential store, never in chat.

  The tick is pinned to the declaration: changing a tool's gate, verb or effect, or the url, audience or claim mode, voids it, and the token is created again. Calls are audited as `upstream_forwarded` and `auto_accepted`.

## Accepted risks

- **n8n chooses participants.** An `n8n_workflow` token that ingests a record with no mapped series or folder chooses the `participants`, and those become the `allowed_emails` of the record. A compromised or careless workflow token can therefore decide who sees an unmapped record. Map every series and folder you use in `raw_audience_map`, which gives audiences instead, and treat the n8n token like a write credential.
- **Scan timing.** Exact vector scans take time in proportion to the number of hidden rows; the timing is the one accepted residual of the no-existence-leak rule.
- **Per-process limits.** The rate limiter is not shared between processes.

## Releasing

Releases follow the `/tld:release` skill. The version lives only in `src/knowledge_hub/__init__.py`.

1. Run `/tld:release`. It moves the `## [Unreleased]` changelog entries under a `## [X.Y.Z] - <date>` heading, bumps `__version__` and opens a `chore/release-<X-Y-Z>` pull request. Review and merge it.
2. The skill dispatches `.github/workflows/release.yml` on `main` with `dry_run` true first. The skill dispatches only once the `ci` workflow has finished on the merge commit; a dry run started earlier fails at **Require the ci checks on this commit**. A dry run checks the changelog heading, that every other check on the commit is green, that `lint`, `test (3.12)`, `test (3.13)` and `image` are among them, builds the arm64 image and runs `docker run --rm <image> --help`. It pushes nothing.
3. When the dry run is green, the skill dispatches the workflow again with `dry_run` false. The `build` job pushes the image to GHCR by digest only.
4. The `publish` job waits for your approval of the `release` environment in GitHub (Actions, the run, "Review deployments"). After you approve, it tags the digest `X.Y.Z`, creates the annotated tag `vX.Y.Z` and a GitHub release whose notes are the changelog section. The workflow has no `skip_checks` input.
5. The image is `ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. There is no `latest` tag. Check the digest with `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`.
6. To roll the new version out, in the compose repo update the `images_track` entry for knowledge-hub to `X.Y.Z`, run `scripts/pin-images.sh` to pin the digest, and deploy as usual.

The first release, 0.1.0, starts from the placeholder version 0.0.0: run `/tld:release 0.1.0`.
