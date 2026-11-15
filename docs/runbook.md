# Knowledge hub runbook

For the operator. Names of variables and keys are in [configuration](configuration.md); the reasons behind the rules are in [design](design.md). Commands run as the `kh` user inside the container, with the `KH_*` environment of the service (see `deploy/compose.example.yaml`). Secrets are files, never variables, and nothing in this document is a real secret or host.

Not run in the cloud, so yours to do and check by hand: the Dockerfile build, and the backup script's `pg_dump` against the protected tables (`deploy/backup-kh.sh`). Check both on the first deploy.

## Install order

1. **Roles and database.** As the Postgres superuser create the database `kh` with the `vector` extension and the four roles: `kh_migrator`, `kh_app` and `kh_indexer` (LOGIN, NOBYPASSRLS, one password each) and `kh_provenance` (NOLOGIN). Also run `GRANT kh_provenance TO kh_migrator`. No role is a superuser and none has BYPASSRLS. The migrations only `GRANT`; they never create roles.
2. **Secrets.** Put one file per secret under the secrets directory and name it in the matching `*_FILE` variable: the three database passwords, `kh_hub_bot_token`, `kh_indexer_token`, `kh_webhook_hmac`, the S3 access and secret key, `kh_cursor_key` and, if you want metrics, `kh_metrics_token`. Write `kh.yaml` (start from `deploy/kh.example.yaml`) and set a real `trust_anchor` for every T0 repo.
3. **Migrate.** `kh migrate`. A changed, already applied migration file refuses the run.
4. **Serve.** `kh serve` (the image does this). The worker starts only when `KH_FORGEJO_URL` is set. `/healthz` answers `ok`.
5. **Ollama.** Start Ollama and pull the embedding model: `ollama pull bge-m3`. Until the model is available, embedding fails; run `kh rebuild` and `kh requeue-embeddings` afterwards.
6. **Index and record the config.** Run `kh rebuild`, then `kh recompute-visibility` **once**. The audience-map loader does not recompute visibility when no `__config__` hash is stored, so until you run it `kh check` reports config drift. Then run `kh check` and expect exit 0.
7. **Webhook.** In Forgejo set an organisation webhook to `http://knowledge-hub:8080/webhooks/forgejo` with the secret from `kh_webhook_hmac`, for push and pull request events.

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
| `frontmatter must not set <key>; the server decides tier and audience` | A file set `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by` or `approved_at`. The file is not indexed and an earlier row of it is removed. Remove the key by a pull request. Other `frontmatter: ...` alerts (missing key, unknown domain, bad `review_by`, `status` not `current` or `superseded`) are fixed the same way. |
| `main of <repo> was rewritten; run kh rebuild after review` | The new head of `main` does not continue from the last indexed sha. The index is unchanged. Inspect the force push in Forgejo. If it was intended, run `kh rebuild --repo <repo>`. If not, restore the branch from a clone. |
| `duplicate id <id> in <repo>: <paths>` | Two files in one repo carry the same frontmatter `id`. Files without a single approved merge are rejected. Give one of them a new id. |
| `supersedes/superseded_by must name a document in the same repo` | A reference points outside the repo. The file is `disputed`. Replace the reference or move the document. |
| `pull request <repo>#<n> does not match candidate <id>` | A `bot/promote/<id>` pull request event named a different repo or number than the candidate row. Nothing was changed. Check the pull request in Forgejo and close it if it is not the bot's. |

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

- **Scoped tokens and gated tools.** Today a scoped token (what n8n uses) can only be given `auto`-gated upstream tools, and write tools must be `popup`. Live n8n writes (`ingest_raw`, `write_derived`) wait for PrivacyFence to allow a scoped token to call a gated tool. Until then the write tools are tested only with signed test claims. Do not work around this with a second auth path.
- **Blessing email matching.** `allowed_emails` rows match the claim's `email`, which PrivacyFence's documentation calls audit-only. Ask PrivacyFence to bless `email` as a visibility input, to set it only for a verified person and to announce any change of it. The three asks are in [design](design.md). If PrivacyFence changes how `email` is filled, who sees attendee-only records changes.

## Accepted risks

- **n8n chooses participants.** An `n8n_workflow` token that ingests a record with no mapped series or folder chooses the `participants`, and those become the `allowed_emails` of the record. A compromised or careless workflow token can therefore decide who sees an unmapped record. Map every series and folder you use in `raw_audience_map`, which gives audiences instead, and treat the n8n token like a write credential.
- **Scan timing.** Exact vector scans take time in proportion to the number of hidden rows; the timing is the one accepted residual of the no-existence-leak rule.
- **Per-process limits.** The rate limiter is not shared between processes.

## Releasing

Releases follow the `/tld:release` skill. The version lives only in `src/knowledge_hub/__init__.py`.

1. Run `/tld:release`. It moves the `## [Unreleased]` changelog entries under a `## [X.Y.Z] - <date>` heading, bumps `__version__` and opens a `chore/release-<X-Y-Z>` pull request. Review and merge it.
2. The skill dispatches `.github/workflows/release.yml` on `main` with `dry_run` true first. A dry run checks the changelog heading, that every other check on the commit is green, builds the arm64 image and runs `docker run --rm <image> --help`. It pushes nothing.
3. When the dry run is green, the skill dispatches the workflow again with `dry_run` false. The `build` job pushes the image to GHCR by digest only.
4. The `publish` job waits for your approval of the `release` environment in GitHub (Actions, the run, "Review deployments"). After you approve, it tags the digest `X.Y.Z`, creates the annotated tag `vX.Y.Z` and a GitHub release whose notes are the changelog section. The workflow has no `skip_checks` input.
5. The image is `ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. There is no `latest` tag. Check the digest with `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`.
6. To roll the new version out, in the compose repo update the `images_track` entry for knowledge-hub to `X.Y.Z`, run `scripts/pin-images.sh` to pin the digest, and deploy as usual.
