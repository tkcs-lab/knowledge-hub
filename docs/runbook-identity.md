# Identity runbook

For the operator. Sign-in, the directory and hub keys. Variable and `kh.yaml` key names are in [configuration](configuration.md); the reasons are in [design](design.md), "Sign-in, OAuth and keys" and "Directory". The rest of the operation is in the [runbook](runbook.md). Nothing here is a real secret or host: secrets are files, never typed into chat, a ticket or a variable.

## Sign-in and OAuth

The hub is its own OAuth authorization server. A person signs in at Google or Keycloak, the hub shows a consent page naming the client and the verified email, and issues a token. A workflow uses a hub key instead (see "Hub keys").

**What to set** (in the `knowledge-hub` service):

| Setting | Value |
|---|---|
| `KH_PUBLIC_URL` | `https://<public hub host>`, no path |
| `KH_ALLOWED_HOSTS` | add the public host (the hub refuses other `Host` headers) |
| `KH_OIDC_ISSUER` | `https://accounts.google.com`, or the Keycloak realm URL |
| `KH_OIDC_CLIENT_ID` | the client id at that provider |
| `KH_OIDC_CLIENT_SECRET_FILE` | a file holding the client secret, mode 0400 |
| `KH_OIDC_ALLOWED_DOMAINS` | the email domains that may sign in |
| `KH_OIDC_HOSTED_DOMAINS` | the Workspace domains; required for Google |

OAuth is on when all four of `KH_PUBLIC_URL`, `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID` and `KH_OIDC_CLIENT_SECRET_FILE` are set and off when none is; some but not all is a startup error (`KH_OIDC_* and KH_PUBLIC_URL must be set together`, exit 2). `KH_OIDC_ALLOWED_DOMAINS` is then required as well, and `KH_PUBLIC_URL` is `https://<host>` with no path. `kh serve` does not print a line that tells you which paths are on (that message is logged at INFO level and no logging is configured for `kh serve`); check the two metadata documents instead (see "Check it"). Leave `KH_DEV_IDENTITY` unset in production.

**Provider clients.** Google: an OAuth client of type Web application whose redirect URI is `https://<public hub host>/oauth/callback`. Keycloak: a confidential client `kh-hub` with the standard flow only, PKCE `S256` and the same redirect URI. For Keycloak keep user registration off, email editing by users off, "Verify email" on and automatic account linking by email off, because the hub trusts `email_verified` from that realm.

**Public paths.** Only these are reachable from the internet; the edge must let them through without a client certificate, because Claude calls them from Anthropic's servers: `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, `/oauth/callback` and `/oauth/consent`. `/webhooks/forgejo`, `/metrics` and `/healthz` stay internal; the hub answers 404 for them on the public name. Rate-limit `/register`, `/authorize` and `/token` per client address at the edge (the hub's own limits are global), and do not log query strings under `/oauth/`.

**Egress.** The hub container needs outbound access only to the issuer's hosts (for Google `accounts.google.com`, `oauth2.googleapis.com` and `www.googleapis.com`). Restrict it by name at an egress proxy or firewall; the hub follows the provider's discovery document.

**Check it.**

1. `curl https://<public hub host>/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server` return JSON; `/metrics` and `/webhooks/forgejo` return 404 or are unreachable.
2. Claude: Settings, Connectors, Add custom connector, `https://<public hub host>/mcp`. You see the provider's sign-in, then the hub's consent page with the client and your email; allow it, then ask Claude to run a hub search. It returns only your groups' content.
3. Claude Code: `claude mcp add --transport http kh https://<public hub host>/mcp`, finish the sign-in in the browser, run a search.

**Troubleshooting.** A sign-in refused with an `email_verified`, domain or hosted-domain message is a provider setting: check the realm or Workspace and `KH_OIDC_ALLOWED_DOMAINS`. A person who signs in but reads nothing is in no group: see "When a person reads nothing" in the runbook. An access token lives `KH_OAUTH_ACCESS_TTL_SECONDS` (one hour); a refresh fails with `invalid_grant` once the person loses every group. To cut one person off at once, remove them from the directory group and run `kh directory sync --once`; a token already issued reads nothing from the next view reload.

## Directory

With `directory.provider` set, group membership comes from Google or Keycloak groups. A separate process, `kh directory sync`, fetches them and stores one snapshot; `kh serve` reads it every `KH_DIRECTORY_VIEW_SECONDS` (30). Static `people` and `groups` in `kh.yaml` keep working next to it. Keep at least one static approver per group, so an approve team is never empty if the directory is switched off.

**Google.**

1. Enable the Admin SDK API in the Cloud project.
2. Create a service account `kh-directory` with no project roles and a JSON key; store the key as the file named by `KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE` (mode 0400, only for the sync service) and delete the download. Note the account's numeric unique ID.
3. Create a dedicated user `kh-directory-reader@<domain>`: no mailbox use, a random password nobody keeps, 2-step verification enforced, no licence. Give it a custom admin role with exactly the privileges Users: Read and Groups: Read.
4. Add domain-wide delegation for the unique ID with exactly these scopes: `https://www.googleapis.com/auth/admin.directory.group.member.readonly` and `https://www.googleapis.com/auth/admin.directory.user.readonly`. Set `KH_DIRECTORY_GOOGLE_SUBJECT` to the reader address (it is required for the Google provider).
5. Create the groups, for example `kh-all@<domain>` (members) and `kh-all-approvers@<domain>` (approvers), and every other kh group. An approver must also be a member, directly or through nesting; otherwise the approver is skipped (an INFO log line `directory: skip ...`, and `kh directory plan` lists it). Keep who-can-join restricted, make only administrators owners and managers (they decide who gets access and who can approve), allow no external members and nest a group only if it is itself restricted. A group that contains the whole domain is refused on purpose.
6. Put the addresses in `kh.yaml`: `directory: {provider: google, domains: [<domain>]}` and, per group, `directory: {google: {members: [...], approvers: [...]}}`.

**Keycloak.**

1. Create a client `kh-directory` with client authentication on and only service accounts roles on. Assign it the `realm-management` roles `view-users`, `query-users` and `query-groups`. Put its secret in the file named by `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE`, set `KH_DIRECTORY_KEYCLOAK_CLIENT_ID` to `kh-directory` and `KH_DIRECTORY_KEYCLOAK_REALM` to the realm name, and set `KH_DIRECTORY_KEYCLOAK_URL` to the internal admin URL (`https`, unless `KH_ENV` is `dev` or `test` or the host is a single-label name such as `keycloak` on the internal admin network); the sync reaches the admin API only over the internal admin network, never through the public edge.
2. Create the groups `kh-<group>` and `kh-<group>-approvers` and put the people in them. No `groups` claim mapper is needed: the hub reads groups from the admin API, not from tokens.
3. In `kh.yaml`: `directory: {provider: keycloak, domains: [<domain>]}` and, per group, `directory: {keycloak: {members: [kh-all], approvers: [kh-all-approvers]}}`.

**The sync service.** Run `kh directory sync --every 300` as its own container from the same image (see `deploy/compose.example.yaml`, service `knowledge-hub-directory`). It is the only container that holds a directory credential, and the only one that needs egress to the provider. On a first deploy the order is `kh setup apply --only postgres`, `kh directory sync --once`, then `kh setup apply` (the sync needs the roles and migration 0006 that the postgres step creates; see the [deployment guide](deployment.md), section "First deploy"). A plain `kh setup apply` with a provider set and no snapshot still runs every step but ends with `! directory: snapshot missing` and exit 2. The sync needs egress to `oauth2.googleapis.com` and `admin.googleapis.com` (Google) or only the internal admin URL (Keycloak). `--every N` (60 to 3600) is the sync interval; it does not read `directory.refresh_seconds`. After a valid change of `kh.yaml` the sync exits with code 75 and `restart: unless-stopped` restarts it, one to two intervals later (see [runbook](runbook.md), "Admin panel").

**Check it.**

- Run `kh directory plan`, `people` and `show` in the `knowledge-hub` container (they read the snapshot with the migrator password); `kh directory sync` runs in the sync container. `kh key` also needs the migrator password.
- `kh directory plan` prints `groups: <m>`, `people: <n>`, `skipped: <k>` and one `skip <group> <role> <who>: <reason>` line per skip. Read every skip. It exits 1 when the snapshot is missing or stale.
- `kh directory people --json` lists the effective people; compare it with the people you expect before relying on it. `kh directory show` prints the provider and the snapshot's age.
- Remove a test person from a group, run `kh directory sync --once`, wait 30 seconds and search as them: the group's content is gone.

**The change guard.** A sync refuses a snapshot whose user count falls below `100 - shrink_guard_percent` percent of the stored one, or more than doubles it (from 10 users up). `--once` exits 3 with `directory: refusing snapshot: <a> users changed to <b>; rerun with --accept-change`. Read the numbers: a real reorganisation is accepted with `kh directory sync --once --accept-change`; an emptied group, a wrong group name or a provider outage is not. The running service (`--every`) keeps the previous snapshot and logs the refusal at ERROR level with the marker `directory-refused` in every cycle. Alert on that marker: a refused change that nobody accepts becomes a directory outage after `max_stale_seconds` (six hours), when every directory-derived group disappears.

**Stale snapshot.** Alert when the gauge `kh_directory_snapshot_age_seconds` is above `2 * refresh_seconds` (assuming `--every` equals `refresh_seconds`) or absent: the series has no value while no snapshot exists or the provider is `none`. Past `max_stale_seconds` the hub ignores the snapshot, logs it once, and only static entries and hub keys keep working. Fix the sync service (credentials, egress, the provider) and run `kh directory sync --once`.

**Joiners and leavers.** Change the group in Google or Keycloak. Directory access follows within about six minutes; Forgejo teams within about twenty-one, when `kh setup apply --only forgejo` next runs (an operator-provided timer, see the [deployment guide](deployment.md), section "Scheduled jobs"). `kh directory sync --once` forces the first part. A directory person needs a Forgejo account whose login is the lower-case local part of the email and whose email equals the directory email; `kh setup` never creates it. Until it exists `kh setup plan` prints `i account <login>: no Forgejo account yet; not added to its teams`; an account with another email prints `! account <login>: email differs from directory; not added` (apply exits 2).

**Rotating the credentials.** Google: create a new key for the service account, write it to the credential file, restart the sync service, run `kh directory sync --once`, then delete the old key in the console. Keycloak: regenerate the client secret in the `kh-directory` client, write the file, restart the sync service. Rotate the OIDC client secret the same way: new secret at the provider, new file content, restart `kh serve`. Write the rotation date down, set an expiry on the Google key if the organisation allows it, and now and then check in the Admin console reporting that tokens issued to the client name only the reader account.

## Hub keys

A hub key is for a workflow (n8n): bound to one active group and a list of tools, sent as `Authorization: Bearer khk_<id>.<secret>`. A key never holds more than its group; `move_item` cannot be given to a key. A key whose group is archived is refused.

- **Create.** `kh key create --group G --tools search,ingest_record --name <workflow> [--expires-days D] [--out FILE]`. The key is shown once, so with `--out FILE` it goes to the file (mode 0600) instead. The file holds the line `key: khk_<id>.<secret>`; the bearer value is the part after `key: `, so remove that prefix before you store it as the header credential. Store it as a header credential in n8n's credential store, never in chat. The tool list defaults to `search,ingest_record`; add `propose_doc`, `usage` and the rest only for a workflow that needs them.
- **List.** `kh key list` shows `id`, `name`, `group`, `tools`, `expires`, `last_used` and `status` (`active`, `expired`, `revoked`); never a secret. A workflow whose `last_used` stays empty is not reaching the hub.
- **Revoke.** `kh key revoke ID`. It takes effect on the next request.
- **Rotate.** Create a second key with the same group and tools, switch the workflow's credential, check one run, then revoke the first.

## Moving from PrivacyFence

The release that brings this runbook removes the PrivacyFence path: the claim header, the `list_audiences` tool, `KH_PF_ISSUER`, `KH_PF_AUDIENCE` and `KH_PF_JWKS_URL`. Do the steps in this order, and expect steps 3 and 4 to follow step 1 at once, because tokens and connectors that went through PrivacyFence stop working at step 1.

1. **Deploy the new image.** `kh migrate` (in compose: the postgres step of `kh setup apply`) applies `0006_identity.sql` and drops the replay table. Remove the `KH_PF_*` variables from the service; the hub ignores them.
   *Rollback.* An older image refuses a database that holds an unknown applied migration, so first undo the migration: from a checkout of the released tag, as `kh_migrator`, run `docker compose exec -T postgres psql -U kh_migrator -d kh < deploy/rollback-0006.sql` (the file is not in the image). The command assumes a Postgres service named `postgres`; the compose example has none, so use the name of your own. It drops every object of `0006`, recreates the replay table and function and deletes the row from the migrations table. The older image also refuses `kh.yaml` keys it does not know and an empty approver list, so in the same change render `kh.yaml` without `directory` and `groups.<g>.directory` and with at least one static approver per group; otherwise it exits 2. Restore the PrivacyFence upstream declaration and the `KH_PF_*` variables for that image.
2. **Directory.** Set `directory` and the per-group references, run `kh directory sync --once` and `kh directory plan`, compare `kh directory people --json` with the people you had in `kh.yaml`, then run the sync service.
   *Rollback.* Set `directory.provider: none`: static people apply again and the snapshot is ignored. Keep one static approver per group.
3. **n8n, right after step 1.** For each workflow create a key with the group and the tools its PrivacyFence token had, change the workflow's credential to a bearer header, check one run, then revoke the old token in PrivacyFence.
   *Rollback.* None is needed: the old token no longer works. Keep the key.
4. **Claude.** Add the hub URL as a custom connector, sign in, test a search, then remove the PrivacyFence connector for the hub.
   *Rollback.* Remove the new connector; the old one works again only with the older image of step 1.

Afterwards the hub's tools are named `search`, `get_doc`, `ingest_record` and so on, without the `kh_` prefix PrivacyFence added; update any prompt or workflow that names the old ones.
