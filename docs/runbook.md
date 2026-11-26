# Knowledge hub runbook

For the operator. Names of variables and keys are in [configuration](configuration.md); the reasons behind the rules are in [design](design.md). Commands run as the `kh` user inside the container (or from the installed wheel), with the `KH_*` environment of the service (see `deploy/compose.example.yaml`). Which container runs which command, and with which secrets, is in the [deployment guide](deployment.md), section "Where each command runs". Identity (sign-in, directory, keys) has its own [runbook](runbook-identity.md). Secrets are files, never variables, and nothing in this document is a real secret or host.

Not run in the cloud, so yours to do and check by hand: the backup script's `pg_dump` (`deploy/backup-kh.sh`) and the first `kh setup` against a real Forgejo. Check both on the first deploy.

## Install

The install is in the [deployment guide](deployment.md), section "First deploy"; day-two work starts here.

## Sign-in, directory and keys

Sign-in with OAuth, the directory providers and hub keys for n8n are in [runbook-identity](runbook-identity.md).

## Admin panel

The admin panel is `kh panel serve`, a separate service of the same image (`knowledge-hub-panel` in `deploy/compose.example.yaml`). Administrators sign in with the company account and manage labels, groups, people and technical repos, watch the indexing pipeline and read the health of the stack, without a shell. It holds the setup admin credentials (Forgejo and Postgres), the `kh_indexer` and migrator passwords, its own OIDC client secret and a session key; `kh serve` holds none of the admin credentials, so keep the panel on a private network or behind an access proxy. The reasons are in [design](design.md#admin-panel).

**First start, in this order.**

1. At the OIDC provider create a client for the panel (Google: a Web application client; Keycloak: a confidential client with the standard flow) and add the redirect URI `https://<panel host>/auth/callback`.
2. Write two secret files: the client secret (`kh_panel_oidc_client_secret`) and a session key of at least 32 bytes (`head -c 48 /dev/urandom | base64 > kh_panel_session_key`). Secrets never go through chat or the repository.
3. Add your email, in lower case, under `admins:` in `kh.yaml`. Only that list decides who gets in, and it is read again on every request; the panel cannot edit it.
4. Start the `knowledge-hub-panel` service with the `KH_PANEL_*` variables of [configuration](configuration.md#admin-panel-kh-panel-serve-only). Every kh container (`knowledge-hub`, `-directory`, `-panel`, one-off commands) must run the same image tag: an older image refuses a `kh.yaml` that has `admins`.
5. Open the panel address, sign in, and check that a person who is not in `admins` sees `not an administrator`.

**The six tabs.**

| Tab | What it does |
|---|---|
| Groups | Per group: members, approvers, directory references and the amount of information (documents by tier, records, chunks, stale and outdated counts, last indexing time). Add, archive, unarchive and remove a group; add and remove members and approvers; add and remove people. |
| Labels | The labels with their document, record and technical repo counts. Add, describe, archive, unarchive and remove a label. |
| Repos | Group repos and technical repos with the indexed head. Add, change `dirs` and `labels` of, and remove a technical repo. |
| Datasets | The datasets with title, groups, column count and estimated rows; per dataset its columns with flags. New dataset, Add column, Edit flags (one toggle per flag), Rename and Drop (asks to confirm). Changes run `kh dataset ...` as the signed-in administrator. |
| Pipeline | The job counts, failed jobs and alerts. Rebuild a repo, Retry a failed job or every dead job, reclaim jobs stuck in `running`, requeue missing embeddings. |
| Health | Checks for `config`, `database`, `migrations`, `hub`, `queue`, `alerts`, `indexed`, `embeddings` and `directory`, plus the buttons `setup plan` and `kh check`. |

**How a change reaches the hub.** Every change has Preview (a dry run that shows the diff of `kh.yaml` and writes nothing) and Apply, which stays disabled until a Preview of the same values succeeded. The panel runs the same `kh label|group|person|repo` commands as the command line, one at a time. After a successful Apply the file has changed; within `KH_CONFIG_WATCH_SECONDS` plus the debounce (about 20 seconds with the default 10) `kh serve` finishes its running jobs and exits with code 75, the container restarts it, and the Health `hub` check turns ok again. While it shows `the hub runs an older kh.yaml than the one on disk` the restart is still pending. A change made only to `admins`, to comments or to formatting restarts nothing. The directory sync service restarts itself the same way, but later: it polls the file once per sync interval and waits two polls, so one to two intervals (5 to 10 minutes at `--every 300`) after a valid change. The panel container does not mount `KH_SETUP_SLACK_WEBHOOKS_FILE` in the example: a change on a group that has `slack_channel` then ends with `! slack hook <repo>: no URL for group <g> in KH_SETUP_SLACK_WEBHOOKS_FILE` and exit 2 until you mount the file. The host directory of `./config` must be writable by the container user, because the panel writes a lock file and a temporary file next to `kh.yaml`.

**When `kh.yaml` is invalid.** The panel fails closed: nobody counts as an administrator while the file is invalid, so requests answer 401 and the sign-in ends with 403 `panel unavailable: kh.yaml is invalid`. `kh serve` keeps serving the config it loaded and does not restart. Fix the file by hand, or with `kh label|group|person|repo` on the command line, then sign in again. Do not hand-edit `kh.yaml` while an operation runs: the command checks the file again before it writes and refuses with `admin: kh.yaml changed while the command ran; run it again` (the panel shows the text; run the operation again). Another change that runs at the same moment gives `Another change is running; try again in a minute`.

**A failed job.** The worker retries a failed job five times. A job that failed all five times is dead and shows in the Pipeline tab with its last error; fix the cause (Forgejo, Ollama, the database), then press Retry. Retry puts the job back with a fresh attempt count and keeps its last error. A job left in `running` for over an hour (the hub stopped mid-job) is put back with Reclaim; a job that waited long in the queue and is really still running can then run twice, which costs only repeated work.

**Rebuild is asynchronous.** The panel never runs `kh rebuild` itself (it needs the embedding model and cannot finish in the timeout on a CPU). Rebuild deletes the indexed head of the repo and queues one sync job; the hub's worker runs it. The Pipeline tab shows the queue and the new head when it is done. From a shell the same is `kh rebuild --repo R --queue`. If the `hub` check warns that the hub runs an older `kh.yaml`, the worker may still use the old repo list: wait for the restart first.

**Accepted risks.** A signed session cookie lives up to `KH_PANEL_SESSION_HOURS` (8 hours by default): removing an email from `admins` takes effect on the next request, but a stolen cookie is not revoked earlier for a person who stays in `admins`. Do not run the panel next to a tool that renders `kh.yaml` from its own registry (the compose repo's kh tab): it would drop `admins` and overwrite `tech_repos`.

## Routine tasks

| Task | Command and notes |
|---|---|
| Check the whole environment | `kh setup plan --config kh.yaml`. Exit 0 means no drift. Run it from a scheduler (for example daily) and alert on exit 1 or 2: it catches a branch-protection or team change made in the Forgejo UI. |
| Re-index after review | `kh rebuild` or `kh rebuild --repo kb-all`. Idempotent. |
| Reassign a repo to another group | Change `repo` of the group in `kh.yaml`, wait for the restart, then run `kh rebuild --repo <repo>` (or wait for the next push): stored documents of that repo are read again under the new group. `kh check` names the drifted documents (`has audience <a>, config says <b>; run kh rebuild --repo <repo>`). |
| Look for drift | `kh check`. Exit 1 means drift; read the printed lines. |
| Daily job | `kh daily`, once a day from a scheduler: recomputes `stale`, purges expired OAuth rows and deletes usage events past `KH_USAGE_RETENTION_DAYS` and dataset history past `KH_DATASET_HISTORY_DAYS`, and deletes records that `delete_records` hid more than `KH_RECORD_PURGE_DAYS` ago (row, chunks and S3 body). |
| Undo a wrong removal | `kh records restore --audience <group> --source <source> [--prefix <source_ref start>] [--since <ISO time with zone>] --by <admin email>`. It prints `{"restored": N}`. Only records that `delete_records` hid and the purge has not claimed come back; the feed sending the `source_ref` again with `ingest_record` also restores one. |
| Find records pending deletion | `search` with `include_outdated=true` shows them; `get_doc` shows the `note` `removed from source`. A record that stays in the purge because its S3 body cannot be deleted shows as `record blobs failed: M` in the `kh daily` line, and `ingest_record` of it answers that the record is being deleted until the store works again. |
| Queue missing embeddings | `kh requeue-embeddings` prints the number queued. It also re-queues a record whose job was left `running` by a worker that stopped mid-job. |
| Change the embedding model or chunker | Change `KH_EMBED_MODEL` or `KH_CHUNKER_VERSION`, restart, run `kh setup apply --only ollama`, `kh rebuild`, then `kh requeue-embeddings`. |
| Check a document before a pull request | `kh lint path/to/file.md`. |

### Datasets

A dataset is a typed table owned by the hub; a workflow fills it (see [feeds](feeds.md)). All commands need `--by <administrator email>` and run as the panel's Datasets tab does. Datasets cannot be renamed or deleted.

- **Create.** `kh dataset create sf_opportunities --title "Opportunities" --audiences sales --by admin@example.com`. Names are lower case letters, digits and `_`, starting with a letter. Each audience must be an active group.
- **Add a column.** `kh dataset add-column sf_opportunities amount --type number --filterable --source-name Amount --by ...`. Types: `text`, `number`, `date`, `bool`, `choice` (`--choices a,b`), `ref` (`--ref <dataset>`). Rename with `rename-column`; `drop-column ... --yes` deletes the column and its values.
- **Flags.** `kh dataset set-flags <dataset> <column> --filterable --no-embed --sensitive --indexed --label L --source-name S`. A `--no-<flag>` clears one. Changing `embed` or `sensitive` refreshes the mirrored search text of every row in the background. A `sensitive` column never reaches search text, the embedder or the title; `query`, `get_doc` and BI logins still return it.
- **Dataset settings.** `kh dataset set <dataset> --title-column name --max-outdated-percent 20 --audiences sales,mgmt --history`. `--no-history` drops the history table and needs `--yes`. Removing a group from `--audiences` deletes that group's search records for the dataset at once (the rows stay in the table).
- **Refresh.** After a flag change the indexer's `dataset_refresh` job rewrites the search records; watch it in the Pipeline tab.
- **Import a file.** `kh dataset import <dataset> file.csv --audience sales --by ... [--key-column key] [--updated-column source_updated_at] [--sync-id S --finish] [--force]`. The first row holds headers: the key column, the timestamp column (the file's modification time when absent) and column names or source names. Batches of 500 are sent as the administrator; a bad row prints `dataset: line <n>: <problem>` and exit 1, and earlier batches stay. `--finish` needs `--sync-id` (exit 2) and refuses an empty file. The last line is a JSON summary `{"inserted", "outdated", "rows", "updated"}`.
- **Group removal.** Row records count as records, so `kh setup apply --prune` and `kh group remove` cannot remove a group that still has dataset rows (`group <g>: still has N records`). They also refuse while the group still owns saved queries, dataset syncs, BI roles or rows in a dataset table (`group <g>: still used by 2 saved queries, 1 BI roles`).
- **History.** With `--history` on, `query(as_of=...)` reads old versions: a plain date means midnight UTC. `kh daily` purges versions older than `KH_DATASET_HISTORY_DAYS`.

### BI access

A BI tool (Metabase and the like) can read one group's rows with a read-only Postgres login. It is off by default: set `KH_BI_ENABLED=true` and `KH_SETUP_PG_ADMIN_DSN_FILE` (a superuser, or a role with CREATEROLE and ADMIN OPTION on `kh_bi`) for the process that runs the command, and allow the BI network in `pg_hba`.

- **Create.** `kh dataset bi-role create sales --by admin@example.com` prints `role kh_bi_<10 characters>` and `password <password>`. The password is shown once and never logged. The login sees only the `kh_data` rows of its group (`SET kh.audiences` or `SET ROLE` change nothing, since the group comes from the login), may only `SELECT`, has 5 connections and a 30 s statement timeout.
- **Rotate.** `kh dataset bi-role rotate kh_bi_<...> --by ...` sets a new password and ends the role's sessions.
- **Drop.** `kh dataset bi-role drop kh_bi_<...> --by ... --yes`.
- **List.** `kh dataset bi-role list [--json]`.
- **Leavers.** When a person who knew a BI password leaves, rotate that role. A group's logins stop working when the role is dropped.
- `kh setup plan` prints `i bi role <name> for <group>` per mapped role. A cluster role matching `kh_bi_<10 characters>` without a mapping prints `! bi role <name> has no mapping`, and `kh setup apply` then exits 2; drop that role or restore its mapping.
- Residuals are listed in [design](design.md), "Datasets".

### Adding a group

Two ways. The command form: `kh person add LOGIN EMAIL` for each new person, then `kh group add NAME --members A,B --approvers A [--repo R] [--slack-channel #C]`. It edits `kh.yaml`, creates the group row, the teams, the repo and its protection, and prints the diff first (`--dry-run` shows it without writing). The file-edit form:

1. Add the group (and any new people) to `kh.yaml`. Every member needs a `people` entry; approvers are a non-empty subset of members.
2. `kh setup plan`, then `kh setup apply`. It creates the group row, the two Forgejo teams, the repo, its protection and the webhook coverage; the other groups are untouched.
3. `kh serve` restarts itself within `KH_CONFIG_WATCH_SECONDS` plus the debounce (a second poll) when the restart policy allows it (`restart: unless-stopped` in the compose example); with `KH_CONFIG_WATCH_SECONDS=0` restart it by hand, because the config is read at start.
4. Put the first files into the new repo through a pull request.

A group's repo cannot change: a changed `repo` is a `!` line. Remove the group (with `--prune`) and add a new one instead. Removing a group is refused while records exist.

### Adding a technical repo

1. In `tech_org` (by hand; `kh setup` creates nothing there) create the pull mirror or fork of the upstream repo. Its default branch is the one indexed.
2. Command form: `kh repo add <repo> [--dirs docs,adr] [--labels product:ember]` (`tech_org` must be set; `dirs` defaults to `docs`; the root `CLAUDE.md` is always indexed). It edits `kh.yaml` and runs the forgejo step for that repo. File-edit form: add `tech_org` (once) and the repo under `tech_repos` in `kh.yaml`, with `labels` and `dirs` if wanted; the group `all` must exist and be active.
3. `kh migrate` (migration `0004_tech_tier.sql`, first time only). With the file-edit form, run `kh setup plan` and `kh setup apply` too. The setup gives the indexer read access, reports the repo's collaborators and adds the tech org webhook.
4. Wait for `kh serve` to restart itself (within `KH_CONFIG_WATCH_SECONDS` plus the debounce; by hand when the watch is off), because the config is read at start; the first sync is queued as soon as `kh serve` starts, then again every `KH_TECH_POLL_MINUTES` (0 switches the poll off); or run `kh rebuild --repo <repo>`.
5. `kh check`. Exit 0 means the head is indexed and every document has tier T0a.

Anyone with push access to the repo puts unreviewed text in front of every caller; read the `i tech repo <repo>: collaborators` line.

### Changing the labels or dirs of a technical repo

1. Command form: `kh repo set <repo> --labels a,b` and/or `--dirs docs,adr` (`--labels ""` clears the labels). File-edit form: edit `labels` or `dirs` of the repo in `kh.yaml` (new labels go under `labels` first).
2. Let `kh serve` restart first (it does itself within `KH_CONFIG_WATCH_SECONDS` plus the debounce; by hand when the watch is off): its worker keeps the old config until then and would undo the change. With the file-edit form run `kh setup apply` too.
3. After the restart, wait for the next push or poll, or run `kh rebuild --repo <repo>`. A label change fetches no file again; a new directory fetches its files, and the documents of a directory that left `dirs` are deleted.

### Removing a technical repo

1. Command form: `kh repo remove <repo>` (`--yes` when it has indexed documents). It removes the entry and deletes its T0a documents and indexed head. File-edit form: remove the repo from `tech_repos` in `kh.yaml`, then `kh setup apply --prune` (without `--prune` it only prints `? tech documents of <repo>: repo is not in tech_repos`). The Forgejo repo is not touched.
2. Make sure `kh serve` restarts now (itself within `KH_CONFIG_WATCH_SECONDS` plus the debounce; by hand when the watch is off): until then its worker still indexes the repo and would bring the documents back, because it keeps the old config.

### Archiving and unarchiving

Command form: `kh group archive NAME` or `kh label archive NAME` (and `kh group unarchive NAME`, `kh label unarchive NAME`). The file-edit form:

1. Set `archived: true` on the group, or add the label to `archived_labels`, in `kh.yaml`.
2. `kh setup plan`, then `kh setup apply`. For a group, `kh setup` also marks its Forgejo repo archived and keeps the teams, but removes team members that `kh.yaml` no longer lists (it never adds any).
3. `kh serve` restarts itself within `KH_CONFIG_WATCH_SECONDS` plus the debounce when the restart policy allows it; with the watch off, restart it by hand.

Nothing is deleted. To unarchive, remove the flag or the `archived_labels` entry, run `kh setup apply`, wait for the `kh serve` restart (or do it by hand when the watch is off), and for a group run `kh rebuild --repo <repo>` once, because pushes while archived were ignored. `kh setup apply --prune` never deletes an archived group or label. To delete one for good, remove it from `kh.yaml` and use `--prune` as before.

### Joiners and leavers

Static people and groups live in `kh.yaml`; with `directory.provider` set, group membership comes from Google or Keycloak as well. For a joiner or leaver, change the group in the directory: directory access follows within about six minutes (the next sync plus the 30 second view reload; `kh directory sync --once` forces it) and Forgejo teams within about twenty-one (the next `kh setup apply --only forgejo`, an operator-provided timer, see the [deployment guide](deployment.md), section "Scheduled jobs"), and no restart is needed. `kh person add LOGIN EMAIL`, `kh group member add GROUP LOGIN`, `kh group member remove GROUP LOGIN` and `kh person remove LOGIN --from-groups` still edit the static entries only; keep them for break-glass people and groups that have no directory reference, and expect `kh serve` to restart itself after such an edit (by hand when the watch is off). A person who was a static member stays one until the entry is removed. A directory person needs a Forgejo account whose login is the lower-case local part of their email and whose email equals the directory email; until then `kh setup plan` prints an `account` line (see [runbook-identity](runbook-identity.md)). Also disable the Forgejo account if they leave the company, and see [runbook-identity](runbook-identity.md) for the directory.

### When a person reads nothing

First check that the email of their sign-in equals their `people` email in `kh.yaml` or their address in the directory (compared in lower case), then run `kh directory plan` and look for a `skip` line naming them. A different address, a missing entry or a missing group all give no groups. For a static edit check that `kh serve` restarted (Health in the admin panel shows the `hub` check); for the directory check `kh directory show` for a snapshot age under `max_stale_seconds`. See [runbook-identity](runbook-identity.md).

### Slack notifications for approvers

1. In Slack, create an incoming webhook for each approver channel (Slack: *Apps → Incoming Webhooks*, one per channel).
2. Write the URLs into the `KH_SETUP_SLACK_WEBHOOKS_FILE` file as `<group>: <url>`, one line each, file mode 600.
3. Set `slack_channel: "#<channel>"` on those groups in `kh.yaml`.
4. `kh setup plan`, expect `+ slack hook <repo>: #<channel>`, then `kh setup apply`.
5. Open a test pull request and check that the channel gets the message.

The message names the pull request's author, which for `propose_doc` is the bot account. To stop the notifications remove `slack_channel` and run `kh setup apply --prune`. To rotate a URL, change the file and run `kh setup apply --only forgejo`.

### Changing T0

People do not push to `main`: direct push is off. They open a pull request (in Forgejo, or through `propose_doc`) and an approver of the group merges it. Required approvals are 0, so an approver may merge their own pull request: review the final diff before merging. After the merge Forgejo calls the webhook and the document is indexed within seconds. A pull request opened by `propose_doc` is on a `bot/propose/<uuid>` branch; one opened by `propose_doc_deletion` is on a `bot/delete/<uuid>` branch and removes the file, so merging it removes the document at the next sync. Closing either unmerged changes nothing.

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

### Calibrating the search similarity floor

`KH_SEARCH_MIN_SIMILARITY` is 0 (off) until it is calibrated for the embedding model, once after the first deploy and again after `KH_EMBED_MODEL` changes.

1. With the floor at 0, run `search` for 10 or more queries that have a known right answer and 10 or more that have none (topics the hub holds nothing about). Use `limit` 50, because hits are ordered by rank and tier weight, not by similarity, so the most similar junk chunk can fall outside the first 10. Note the highest `similarity` among the hits of each query. `similarity` is null for every hit when the search runs in full-text-only mode (a group it covers may not be embedded, see `KH_EMBED_HTTP_ALLOWED_AUDIENCES`), so calibrate with a caller whose groups may all be embedded.
2. Take the lowest of the known-good values and the highest of the no-answer values. If the first is above the second, set the floor between them, nearer the no-answer value; otherwise the model does not separate them and the floor stays 0.
3. Set `KH_SEARCH_MIN_SIMILARITY` in the `knowledge-hub` service environment and restart `kh serve` (a change of the environment is not a change of `kh.yaml`, so it does not restart by itself). Run the no-answer queries again: they return no hits (a hit that still appears matches the query's words by full text; one whose `similarity` is below the floor and that has no word match would be a bug). If a no-answer query still returns a vector-only hit, raise the floor above that hit's `similarity` and repeat; if a good query loses its answer, set the floor back to 0.

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

An administrator (an email in `admins` of `kh.yaml`) moves an item; Claude cannot (the old `move_item` tool is gone). Run `kh move rec:<uuid> --from mgmt --to all --by admin@example.com --config kh.yaml` in the hub container, or use the "Move an item" card on the panel's Pipeline tab (it runs the same command as the signed-in administrator). You need not hold either group. The command prints one JSON line. Exit codes: 0 moved; 1 refused (`move: <text>`, including `move: <email> is not an administrator`); 2 bad arguments or config; 3 the removal pull request of a document could not be opened (run the command again). A record moves at once and keeps its id. A document moves with two pull requests by the bot:

1. Merge the add pull request ("Move in: ...") in the target repo as an approver of the target group.
2. Then merge the removal pull request ("Move out: ...") in the source repo as an approver of the source group. Until the removal is merged the document is in both groups; if it is merged first it is missing from both.

The moved document gets a new `doc:` id once the target repo is indexed. A retry returns the open pull requests, or opens only the removal when the add is open or already merged (the answer's `note` says when the removal could not be opened). The answer lists `dropped_supersedes` (links the document had, which a move drops) and `revived` (documents of the source group that become current again once the removal is merged).

Refusals and what to do:

- `a record with this source and source_ref was moved out of this audience`: a workflow's `ingest_record` re-sent a record that was moved; do not re-send it, or move the record back (that clears the tombstone).
- `another item names this one as its replacement or successor; clear that first`: clear the `replaced_by` of the record, or the `superseded_by` of the document, then run `kh move` again.
- `this document cannot be moved by the tool; move it by hand`: the text has something the tool cannot render back unchanged (for example HTML-like text in the body); copy the file between the repos with two pull requests yourself.
- `the target group already has an item with this source and source_ref, path or id`: rename or remove the item in the target group first.

A move is not a purge: the old repo's history, past usage queries and backups keep the old copy.

## Backup and restore

Documents and chunks are rebuilt from Forgejo, groups and labels from `kh.yaml`, the directory snapshot by the next sync. `deploy/backup-kh.sh` writes `kh-t2t3-<date>.dump` (custom format, INSERT statements) of `kh.records`, the dataset catalog and saved queries (`kh.datasets`, `kh.columns`, `kh.saved_queries`, `kh.dataset_syncs`, `kh.bi_roles`) and every table of schema `kh_data` (rows and history), and mirrors the blob bucket into `<BACKUP_DIR>/blobs/`; back both up off the box (restic).

**Not in the backup and not rebuildable.** `kh.api_keys`: every hub key is lost; create the keys again with `kh key create` and switch every n8n credential. `kh.oauth_*`: every Claude connector signs in again. `kh.record_moves`: do not re-send a record that was moved, because a re-sent record is accepted again. `kh.usage_events`: lost.

1. Start with an empty database: run `kh setup apply --only postgres --config kh.yaml` when `directory.provider` is set (a plain apply ends with `! directory: snapshot missing` and exit 2), which creates the roles, the database, the schema and the groups; otherwise `kh setup apply --config kh.yaml` (or create the roles and run `kh migrate`), which also creates the labels.
2. As the Postgres superuser restore the records and datasets: `pg_restore --no-owner -d kh kh-t2t3-<date>.dump` (INSERT format, so RLS does not block it). It brings back the row records of datasets together with their rows, so search finds them again after step 5 without a refresh. A BI login's role itself is not in the dump, but its mapping is: after a restore into a new cluster run `kh dataset bi-role list`, `kh dataset bi-role drop <role> --yes` for each stale mapping (it works when the role no longer exists) and create the logins again with `kh dataset bi-role create`. Because `--no-owner` makes the superuser own the restored data tables, hand them back to the migrator, or later `kh dataset` DDL fails with "must be owner": `psql -d kh -c "DO \$\$ DECLARE t text; BEGIN FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'kh_data' LOOP EXECUTE format('ALTER TABLE kh_data.%I OWNER TO kh_migrator', t); END LOOP; END \$\$"`.
3. Copy the blobs back: `aws s3 sync "<BACKUP_DIR>/blobs/" s3://<bucket> --endpoint-url <endpoint>`.
4. If `directory.provider` is set run `kh directory sync --once`, then `kh setup apply --config kh.yaml`, then `kh rebuild` for the git-derived tables.
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
2. The skill dispatches `.github/workflows/release.yml` on `main` with `dry_run` true first, once the `ci` workflow has finished on the merge commit. A dry run checks the changelog heading, that `lint`, `test (3.12)`, `test (3.13)` and `image` are green, builds the arm64 image, the amd64 image (under QEMU) and the wheel and smoke-tests each. It pushes nothing; look under Artifacts for the item `wheel`.
3. When the dry run is green, the skill dispatches the workflow again with `dry_run` false. The `build` job pushes one image index for linux/arm64 and linux/amd64 to GHCR by digest only, and fails unless the index holds both platforms.
4. The `publish` job waits for your approval of the `release` environment in GitHub (Actions, the run, "Review deployments"). After you approve, it tags the digest `X.Y.Z`, creates the annotated tag `vX.Y.Z` and a GitHub release whose notes are the changelog section. The release carries the wheel `knowledge_hub-X.Y.Z-py3-none-any.whl`, `SHA256SUMS` and `deploy/kh.example.yaml`.
5. The image is `ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. There is no `latest` tag. Check the digest with `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. The tag is one index for both platforms; Docker pulls the one that matches the host.
6. To roll out, follow [deployment guide](deployment.md), section "Upgrade".

Changes to `.github/workflows/release.yml` (the wheel, the amd64 image) are a protected path: each reaches `main` as a protected-change pull request, reviewed and merged by a person.

## Accepted risks

- **Two groups, the wider one.** A caller holding two groups can `propose_doc` (or `propose_doc_deletion`) into the wider one (an approver still merges it) and `mark_record_outdated`; `ingest_record` is for hub keys only and moves are administrator commands. See [design](design.md), "Accepted residuals".
- **Leaked bot token.** It reads every T0 repo and can open pull requests and push to open pull-request branches, but cannot merge.
- **Zero required approvals.** The approver must review the final diff.
- **Scan timing.** Exact vector scans take time in proportion to the number of hidden rows.
- **Per-process limits.** The rate limiter is not shared between processes.
- **Backups omit hub keys, OAuth state and move tombstones.** See "Backup and restore"; the dump holds `kh.records`, the dataset tables and the dataset catalog only.
