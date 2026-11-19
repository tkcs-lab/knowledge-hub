# Plan 58: Hub-owned sign-in (OAuth), directory groups from Google or Keycloak, and hub keys for n8n

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator gives the hub a public URL and one upstream sign-in (Google in production, Keycloak in test), lists in `kh.yaml` which Google groups or Keycloak groups make up each kh group, and runs `kh directory sync` as a small second service. In Claude a person adds the hub URL as a connector, signs in once with Google or Keycloak, approves the client and then reads exactly the groups the directory gives them. For n8n, `kh key create --group all --tools search,ingest_record --name nightly` prints a key once; `kh key list` and `kh key revoke <id>` manage keys. Switching provider is a config change (`directory.provider`, the provider's credentials and `KH_OIDC_*`), not a code change; `kh directory plan|people|show` report what the directory gives.

**Scope:** Adds an OAuth authorization server inside the hub (registration with a redirect allowlist, consent, PKCE, opaque hashed tokens), an upstream OIDC sign-in, the directory providers `google` and `keycloak` behind one interface and one pure rule module, a Postgres snapshot, hub keys, migration `0006` with a rollback script, and the effective membership in `kh setup`, `list_audiences` and the PrivacyFence claim path. `KH_PF_*` become optional, `groups.<g>.approvers` may be empty when the directory supplies approvers, `legacy_privacyfence` keeps PrivacyFence's table limits, and `kh daily` purges expired identity rows. The PrivacyFence claim path, `list_audiences` and existing PrivacyFence-minted n8n tokens keep working while `KH_PF_*` is set. Tiers, row-level security and tools are unchanged. The privacyfence and compose repositories are not touched; the compose changes are a hand-off document.

**Security:** The hub becomes internet-facing and its own token issuer. Directory credentials live only in the separate sync process. Delegation is limited to two read-only Google scopes and a dedicated reader account. PrivacyFence popups no longer guard people who connect by OAuth; OAuth scopes and the client's own tool approval replace them.

**Review focus:** the authorize, consent and redirect rules (section 3.9); the sync process boundary and fail-closed staleness (3.6); the single caller middleware with three credential types and the legacy path (3.10, 3.11); Google delegation against a direct role (3.5).

## 1. Goal

Today a person reaches the hub only through PrivacyFence, and a person's groups are whatever `kh.yaml` lists for their verified email; a Keycloak-reading Ansible role in the compose repo rewrites that list at converge time. The owner wants the hub independent of PrivacyFence: MCP clients (Claude) connect to the hub with OAuth, the hub signs people in against Google or Keycloak by OIDC, and a directory provider turns Google or Keycloak group membership into the kh groups and approvers a person holds. n8n stops using PrivacyFence-minted tokens and uses keys the hub issues, each bound to one group and a tool list. Tracking issue: https://github.com/tkcs-lab/knowledge-hub/issues/58.

## 2. Current state

- **One caller path.** `src/knowledge_hub/auth/claims.py:153-289` `ClaimMiddleware` is the only authentication: it buffers the body, verifies the `X-PrivacyFence-Claim` header with the vendored verifier (`auth/pf_claim_verify.py`, claim bound to the body hash) and refuses with a bare 401. `claude_client` groups come from `KhConfig.audiences_for_email` (`config.py:282`), `n8n_workflow` groups from the claim's audiences (`claims.py:112-121`), `service` may only call `list_audiences`. `app.py:62-79` wraps the SDK's `/mcp` endpoint in it. `deps.py:62-77` `make_verifier` raises unless `KH_PF_*` or `KH_DEV_IDENTITY` is set.
- **Groups come from `kh.yaml` only** (`config.py:62-82`, `:140-180`), read at start; `approvers` must be non-empty (`config.py:169`) and a subset of `members` (`:177`). `kh setup` builds the Forgejo teams from `gc.members` and `gc.approvers` (`setup/forgejo.py:164, 204-209, 264`) and prints `! account X: does not exist` for a login without a Forgejo account (`:278`); it never creates users.
- **PrivacyFence table limits** (200 audiences, 20000 addresses, 1,000,000 characters) are enforced when `kh.yaml` loads (`config.py:181-199`); `tools/audiences.py:19` answers `list_audiences` from the static config.
- **No public surface and no egress.** In the compose stack (tkcs-work-compose, read at commit `7fc2c6b`) the hub is on the internal networks `db`, `kh`, `pf-kh`; PrivacyFence signs people in through Keycloak 26.4.0, which brokers Google per domain with the hosted domain set. The role `roles/kh_directory` reads Keycloak groups `kh-<group>` and `kh-<group>-approvers` at converge, skips disabled, unverified, foreign-domain and invalid-login users, ignores approvers who are not members, merges static entries and renders `kh_people`/`kh_groups`; `forgejo_kh` creates one Forgejo account per person; Forgejo signs in through PrivacyFence's OIDC.
- **Rows keyed by `Caller.sub`:** the rate limiter, `records.created_by_sub` (RLS insert check, `0001_baseline.sql:233`), the usage searcher hash (`usage.py:84`), cursors, and the `Requested-by` lines of pull requests. The definer functions of `0003` and `0005` read `kh.caller_sub()` only as an audit value.
- **The SDK** (`mcp==2.3.0`) ships `mcp.server.auth`: `OAuthAuthorizationServerProvider`, `create_auth_routes`, `create_protected_resource_routes`, DCR, revoke and metadata handlers. The hub does not use them. `docs/design.md` lists "A second auth path" and "The SDK's `auth=` hook" as rejected; the owner's 2026-11-19 direction in issue #58 supersedes the first, and 3.9 says why the second still stays rejected.
- **Tests and CI.** `tests/unit` (marker `unit`) and `tests/integration` (Postgres 16 with pgvector); `tests/unit/test_import_boundary.py` forbids the serving app from importing `admin`, `setup`, `forgejo.admin` and `ruamel`; `tests/unit/test_docs_links.py` caps markdown files at 400 lines; `.github/workflows/ci.yml` is a protected path.

## 3. Design

### 3.1 Decisions and what was rejected

| Decision | Rejected alternative and why |
|---|---|
| The hub is its own OAuth authorization server and delegates only the password step to one upstream OIDC provider chosen by config. | Keycloak as the only authorization server: Google, the production provider, has no dynamic registration and no RFC 8707 audience, and "switch by config" would break. A broker in front of the hub: a second service the owner does not want (the point is independence). |
| One bearer path for people (OAuth tokens) and n8n (hub keys), opaque secrets stored as SHA-256, checked per request against Postgres. | Signed JWT access tokens: revocation within minutes needs a lookup anyway. A second middleware: duplicate body and error handling. |
| Groups come from a directory lookup, not a token claim, for both providers; the answer is a snapshot of whole groups. | Claim `groups`: Google ID tokens carry none, and `kh setup`, Forgejo teams and `list_audiences` need the whole table, including people who never signed in. Per-login live lookup: a Google call on every request and nothing for Forgejo teams. |
| Resolution runs in a separate process (`kh directory sync`) with its own credentials; `kh serve` only reads the snapshot table. | A refresher inside `kh serve`: puts a delegation key or a Keycloak admin secret in the internet-facing process and breaks "the hub never holds admin credentials" (`docs/configuration.md`). The converge-time model: leavers wait for the next converge. |
| `groups.<g>.directory.<provider>.{members,approvers}` in `kh.yaml`. | The owner's example `groups.<g>.google.*`: `GroupConfig` would have to name providers. Both identifier sets can sit side by side, so switching is `directory.provider` only. |
| Dynamic registration with a redirect allowlist, a consent page and caps. | Open registration without consent (confused deputy). Pre-registration only: claude.ai cannot be configured without a registered client unless the operator knows its settings; `kh client` is not built now. CIMD: needs the hub to fetch arbitrary URLs (SSRF) and the SDK does not advertise it; Claude falls back to registration. |
| `sub` of an OAuth person is `u:<lower email>`; of a key `key:<id>`. | `<provider>:<upstream sub>`: switching provider would reset usage counts and cursors. |
| PrivacyFence stays an optional legacy path (`KH_PF_*`). | Removing it now: the owner asked that `list_audiences`, the service claim and existing n8n tokens keep working until the follow-up plan. |

### 3.2 Identity

| Caller | `principal_type` | `sub` | `email` | Groups |
|---|---|---|---|---|
| Person with an OAuth token | `claude_client` | `u:<lower email>` | the token's email | `Membership.audiences_for_email(email)` at every request |
| Hub key | `n8n_workflow` | `key:<id>` | none | the key's single group; a key whose group is not active is refused (401) |
| PrivacyFence claim | unchanged | unchanged | unchanged | unchanged, but resolved through `Membership` |

A record created through the claim path and one created by the same person through OAuth carry different `created_by_sub`; nothing compares them (checked: only the RLS insert check and audit text read it). Cursors and usage hashes of one person differ by path. This is accepted and documented.

### 3.3 Settings and `kh.yaml`

Secrets only as `*_FILE` variables. New `Settings` fields (`settings.py`), all optional:

| Variable | Default | Rule |
|---|---|---|
| `KH_PUBLIC_URL` | none | `https://host` with no path; `http://` allowed only when `KH_ENV` is `dev` or `test` and the host is `localhost`, `127.0.0.1` or `[::1]` (the SDK's rule); no userinfo, query, fragment or trailing slash. The authorization server issuer, and the resource is `<KH_PUBLIC_URL>/mcp`. |
| `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID`, `KH_OIDC_CLIENT_SECRET_FILE` | none | OAuth is on only when `KH_PUBLIC_URL` and all three are set; some but not all: `KH_OIDC_* and KH_PUBLIC_URL must be set together` (exit 2). |
| `KH_OIDC_ALLOWED_DOMAINS` | empty | Comma list of lower-case email domains; required when OAuth is on (`KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on`). |
| `KH_OIDC_HOSTED_DOMAINS` | none | Comma list. When set, the ID token's `hd` must be in it and the email domain in `KH_OIDC_ALLOWED_DOMAINS`. Required (exit 2: `KH_OIDC_HOSTED_DOMAINS is required for a Google issuer`) when the host of `KH_OIDC_ISSUER` is `accounts.google.com`. |
| `KH_OAUTH_DCR` | `true` | Dynamic registration route mounted or not. |
| `KH_OAUTH_REDIRECT_ALLOWLIST` | `https://claude.ai/api/mcp/auth_callback,http://localhost,http://127.0.0.1` | Comma list, parsed with `urllib.parse.urlsplit`. An `https` entry matches only the identical URI (no userinfo, query or fragment). An `http://localhost` or `http://127.0.0.1` entry matches a URI whose scheme is `http`, whose `hostname` is exactly that name, with no userinfo and no fragment, on any port and path. Checked at registration, at `/authorize` and in the callback. |
| `KH_OAUTH_ACCESS_TTL_SECONDS` | `3600` | 300 to 86400. |
| `KH_OAUTH_REFRESH_TTL_DAYS` | `30` | 1 to 365. |
| `KH_OAUTH_MAX_CLIENTS` | `200` | Counts clients that completed a token exchange plus unauthorised ones younger than one hour; more is refused. Unauthorised clients are purged after one hour and clients unused for 30 days by `kh daily`. |
| `KH_OAUTH_REGISTER_PER_MIN` | `30` | Registration requests per minute for the whole hub. |
| `KH_OAUTH_AUTHORIZE_PER_MIN` | `600` | Requests per minute, for the whole hub, to `/authorize`, `/oauth/callback` and `/oauth/consent` together (429 above it). |
| `KH_DIRECTORY_VIEW_SECONDS` | `30` | How often `kh serve` re-reads the snapshot (5 to 600). |
| `KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE`, `KH_DIRECTORY_GOOGLE_SUBJECT` | none | Service account key (JSON) and the admin address it impersonates. |
| `KH_DIRECTORY_KEYCLOAK_URL`, `KH_DIRECTORY_KEYCLOAK_REALM`, `KH_DIRECTORY_KEYCLOAK_CLIENT_ID`, `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE` | none | Base URL (no trailing slash; `https`, unless `KH_ENV` is `dev` or `test` or the host is a single-label name such as `keycloak`, a service on the internal admin network), realm, service-account client. |
| `KH_DIRECTORY_KEYCLOAK_SUBGROUPS` | `false` | `true` also reads the members of subgroups (3.5). |

`KH_PF_ISSUER`, `KH_PF_AUDIENCE`, `KH_PF_JWKS_URL` become optional with no other requirement: `make_verifier` returns `None` when they are absent, and `build_deps` logs one line `auth paths: privacyfence=<on|off> oauth=<on|off> keys=on dev_identity=<on|off>`. With every path off, only keys work and every other request is refused 401. The hub refuses to start (exit 2) when `KH_DEV_IDENTITY` is set and OAuth is on. A request whose `Authorization` header is not a well-formed `Bearer <token>` is refused 401, never given the dev identity.

New `kh.yaml` keys (non-secret policy; unknown keys stay refused):

```yaml
legacy_privacyfence: true       # bool, default true: keep PrivacyFence's table limits (3.11)
directory:
  provider: none                # none | google | keycloak (default none)
  domains: [tkcs.name]          # lower-case email domains a person may have; required when provider is not none
  refresh_seconds: 300          # 60..3600, how often `kh directory sync --every` fetches
  max_stale_seconds: 21600      # >= 2 * refresh_seconds, <= 604800
  shrink_guard_percent: 50      # 1..99
groups:
  all:
    members: [alice]            # static, unchanged
    approvers: [alice]
    directory:
      google:   {members: [kh-all@tkcs.name], approvers: [kh-all-approvers@tkcs.name]}
      keycloak: {members: [kh-all], approvers: [kh-all-approvers]}
```

`GroupDirectoryConfig` is `{members: list[str], approvers: list[str]}`; a group's `directory` is a map from provider name (`^[a-z][a-z0-9_]{0,30}$`) to it; entries are non-empty strings of at most 254 characters, unique within a list. Config validation checks only the shape; an unknown `directory.provider` is refused by `build_provider` with `directory: unknown provider <x>` (exit 2). `groups.<g>.approvers` may be empty only when some `directory.<p>.approvers` of the group is non-empty (otherwise the existing error `group <g> needs at least one approver` stays); a static approver who is not a static member is still refused unless the group has a `directory.<p>.members` list, in which case `effective` decides (3.4). The 200 / 20000 / 1,000,000 check at load runs only when `legacy_privacyfence` is true.

### 3.4 Directory core (pure, `src/knowledge_hub/directory/`)

`model.py`:

```python
@dataclass(frozen=True)
class DirectoryUser:  email: str; enabled: bool; email_verified: bool; username: str = ""      # no defaults: a missing flag is a parse error, never a pass
@dataclass(frozen=True)
class GroupFetch:     members: tuple[DirectoryUser, ...]; approvers: tuple[DirectoryUser, ...]
@dataclass(frozen=True)
class Skip:           group: str; role: str; who: str; reason: str      # role is "member" or "approver"
@dataclass(frozen=True)
class Membership:
    people: Mapping[str, str]                 # login -> lower email
    members: Mapping[str, frozenset[str]]     # group -> logins
    approvers: Mapping[str, frozenset[str]]
    skips: tuple[Skip, ...] = ()
    def audiences_for_email(self, email: str | None, is_active: Callable[[str], bool]) -> frozenset[str]: ...
    def audience_table(self, is_active: Callable[[str], bool]) -> dict[str, list[str]]: ...  # group -> sorted emails
    @staticmethod
    def from_config(config: KhConfig) -> Membership: ...                    # static only; the approver-must-be-a-member rule applies here too
class MembershipView(Protocol): def current(self) -> Membership: ...
```

`rules.py` (no I/O):

- `login_from_email(email: str) -> str | None`: lower-case and trim; exactly one `@`; the local part must match `^[a-z0-9][a-z0-9_.-]{0,38}$`.
- `classify(user, domains, reserved) -> str`, first match wins, `""` means keep: `disabled` (not `enabled`), `email not verified`, `no valid email` (not exactly one `@`), `domain not allowed` (domain not in `domains`), `not a valid login`, `bot or reserved login` (login in `{config.bot_login, config.indexer_login, "owners"}`, case-folded). These are the exact reasons of the compose role, in its order.
- `effective(config, fetched: Mapping[str, GroupFetch] | None, domains: Sequence[str]) -> Membership`:
  1. Start from the static people, members and approvers.
  2. For every active group present in `fetched`, classify members then approvers. A skipped user becomes a `Skip`.
  3. Login collision: a login that more than one distinct email maps to is kept for the static email if a static `people` entry holds it (the directory email is skipped with reason `login shared by several emails`); between directory emails both are skipped with the same reason. A directory email that a static `people` entry already holds under another login takes that static login (one email never has two logins).
  4. Passing directory members join `people` and the group's members.
  5. Approvers are the static approvers plus the passing directory approvers; each must be in the group's final members, otherwise a `Skip` with reason `approver is not a member` and the person is not an approver.
  6. `skips` is sorted and de-duplicated by `(group, role, who, reason)`.
- `check_audience_limits(table)` is a function of `config.py` (the three existing messages of `config.py:181-199`, moved into one function); `sync.py` and `MembershipCache` call it, `rules.py` does not.
- `fetched is None` gives the static membership.

Archived groups take no directory data. `Membership.audiences_for_email` equals the current `KhConfig.audiences_for_email` on a static membership; the old method stays unchanged.

### 3.5 Providers (`directory/provider.py`, `google.py`, `keycloak.py`)

```python
@dataclass(frozen=True)
class GroupRefs: members: tuple[str, ...]; approvers: tuple[str, ...]
class ProviderError(Exception): ...
class DirectoryProvider(Protocol):
    name: str
    async def fetch(self, refs: Mapping[str, GroupRefs]) -> dict[str, GroupFetch]: ...   # whole fetch or ProviderError
    async def aclose(self) -> None: ...
def build_provider(name: str, settings: Settings, *, http: httpx.AsyncClient | None = None) -> DirectoryProvider
```

`build_provider` maps `google` and `keycloak` to `knowledge_hub.directory.google` and `.keycloak` with `importlib` (each module has `make(settings, http)`); the core never imports a provider. `ProviderError` texts hold the endpoint name and status code only, never a response body, an assertion or a secret. A fetch either returns every requested group or raises `ProviderError`; a missing group, a 4xx or 5xx after retries, a malformed page, or a refusal is never an empty list. Retry: 429 and 5xx, three tries, 1 s then 3 s, total deadline 60 s.

**Google.** Facts (research r-20261119-1, 2026-11-19): Cloud Identity `searchTransitiveMemberships` needs a paid edition and returns no suspended flag (https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveMemberships); Directory `members.list` with `includeDerivedMembership=true` covers nested members without one (https://developers.google.com/workspace/admin/directory/reference/rest/v1/members/list); `suspended` and `archived` are fields of the Directory user (https://developers.google.com/workspace/admin/directory/reference/rest/v1/users); the Directory default quota is 2,400 queries a minute per user per project (https://developers.google.com/workspace/admin/directory/v1/limits). Only the Cloud Identity API is documented to work with a role assigned to the service account directly (https://docs.cloud.google.com/identity/docs/how-to/setup); no source confirms the same for the Directory API. Decision: **domain-wide delegation** impersonating a dedicated reader account, with exactly two scopes, `https://www.googleapis.com/auth/admin.directory.group.member.readonly` and `https://www.googleapis.com/auth/admin.directory.user.readonly`. The reader account holds a custom admin role with only Groups read and Users read, and no other privilege. A direct-role variant is a follow-up spike (Risks). Delegation lets the key impersonate any user of the domain: the two scopes, not the reader account's role, are the real limit, and design.md records that as a residual (3.17).

- Token: sign a JWT assertion with PyJWT (`RS256`, `iss` the key file's `client_email`, `sub` `KH_DIRECTORY_GOOGLE_SUBJECT`, `scope` the two scopes space-joined, `aud` the key file's `token_uri` which must equal `https://oauth2.googleapis.com/token`, `exp` 1 h) and POST it as `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`; cache the token until five minutes before expiry. No Google client library.
- Members: for each reference, `GET https://admin.googleapis.com/admin/directory/v1/groups/<urlencoded email>/members?includeDerivedMembership=true&maxResults=200`, following `nextPageToken`. A row of type `GROUP` is ignored (its members are in the list already). A row of type `CUSTOMER` (everyone in the domain) makes the whole fetch fail with `group <ref> contains the whole domain`. A `USER` row with `status` other than `ACTIVE` is not enabled. External members come through as users and are removed by the domain rule.
- Users: one paged `GET https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&maxResults=500&fields=nextPageToken,users(primaryEmail,suspended,archived,aliases,nonEditableAliases)` per fetch. A member email is enabled when it is a primary address or alias of a user who is neither suspended nor archived; a member whose address is in a configured domain but is absent from the list is not enabled. `email_verified` is always true (Workspace-managed address).
- Group references are group email addresses; `ProviderError` on 404.

**Keycloak.** Token by client credentials at `<url>/realms/<realm>/protocol/openid-connect/token`; the client needs the realm-management roles `view-users`, `query-users` and `query-groups` (the compose admin tool's client holds the same) and reaches Keycloak only over the internal admin network. A reference starting with `/` is a group path (`GET /admin/realms/<realm>/group-by-path/<path>`); any other is a top-level group name (`GET /admin/realms/<realm>/groups?search=<name>&exact=true`, keeping the result whose `path` is `/<name>`); none found is a `ProviderError`. Members: `GET /admin/realms/<realm>/groups/<id>/members?briefRepresentation=false&first=<n>&max=100` until a short page. These are the direct members only, as in the compose role being replaced. With `KH_DIRECTORY_KEYCLOAK_SUBGROUPS=true` the provider also walks `GET /admin/realms/<realm>/groups/<id>/children?first=<n>&max=100` recursively (a visited set; depth over 10 raises `ProviderError`) and adds each child's members; it is off by default because anyone who manages a subgroup could otherwise grant access to the parent. References must match `^/[A-Za-z0-9 _-]{1,100}(/[A-Za-z0-9 _-]{1,100})*$` (a path) or `^[A-Za-z0-9][A-Za-z0-9 _-]{0,100}$` (a top-level name), each path segment is percent-encoded, and `KH_DIRECTORY_KEYCLOAK_URL` must be `https` unless `KH_ENV` is `dev` or `test` or its host is a single-label name (the compose service `keycloak` on the internal admin network). `DirectoryUser` takes `email`, `enabled`, `emailVerified`, `username`. Nothing in this module or in the core names Keycloak outside `keycloak.py`, its tests and the settings.

### 3.6 Snapshot, sync process, staleness

- **Snapshot.** One row of `kh.directory_snapshot` (3.8): `{"version": 1, "provider": "<name>", "groups": {"<g>": {"members": [{"email","enabled","email_verified","username"}, ...], "approvers": [...]}}}` holding the raw fetched users, so rule changes (domains, reserved logins) apply without a refetch. `fetched_at` is set by the database.
- **`kh directory sync [--once | --every N] [--accept-change]`** (`directory/sync.py`, `directory/cli.py`): connects as `kh_indexer`, builds the provider, and per run: fetch (refs from `config.groups` of active groups for the active provider; a group with no reference for the provider is left to static data); build the snapshot; `check_audience_limits` on the effective table when `legacy_privacyfence`; apply the change guard (when a snapshot is stored: refuse when the total user count of the new snapshot is below `100 - shrink_guard_percent` percent of the stored one, or more than double it when the stored count is at least 10, unless `--accept-change`; the first run has no stored snapshot and is not guarded); store with `kh.directory_snapshot_put`; log one line `directory: <provider> <n> users in <m> groups, <k> skipped`, each skip, and for every group whose approver set grew `directory: approvers added in <group>: <logins>`. `--every N` (default `refresh_seconds`) loops; an error in a run is logged and the previous snapshot stays; the exit code of `--once` is 0, 1 with `directory: <provider>: <reason>`, or 3 for a refused change: `directory: refusing snapshot: <a> users changed to <b>; rerun with --accept-change` (`--every` logs it at ERROR level with the marker `directory-refused` in every cycle).
- **Serving.** `directory/store.py` `MembershipCache(app_pool, config, settings)`: `refresh()` reads `kh.directory_snapshot_get()`, runs `effective`, keeps the result; `current()` returns it; when `now - fetched_at > max_stale_seconds` (or there is no row while a provider is configured) `current()` returns the static membership, logs once per transition and sets the gauge `kh_directory_snapshot_age_seconds`. `kh serve` refreshes every `KH_DIRECTORY_VIEW_SECONDS`. `current()` compares `now` with the `fetched_at` of the snapshot it adopted, so a failing `refresh()` (database error, limit check) cannot extend freshness, and a snapshot whose `provider` differs from `config.directory.provider` or whose `version` is not 1 counts as missing. A stale or missing snapshot therefore removes every directory-derived group (fail closed, documented as "a directory outage over `max_stale_seconds` removes directory access"); static entries and keys keep working.
- **Other commands.** `kh directory plan` prints `groups: <m>`, `people: <n>`, `skipped: <k>` and one line `skip <group> <role> <who>: <reason>` per skip, and exits 1 when the snapshot is missing or stale. `kh directory people [--json]`: `[{"login","email","source","groups":[...],"approver_of":[...]}]` with `source` `static` or `directory` (the hand-off for compose's account creation). `kh directory show` prints `provider: <name>` and `snapshot: <age>s old, <n> users in <m> groups` or `snapshot: none`. `kh daily` also runs `purge_identity` (3.8).
- **`load_membership(config, settings) -> tuple[Membership, float | None]`** (async; the membership and the snapshot age in seconds) is what `kh setup` and the admin commands use. With `directory.provider` `none` it returns the static membership and `None` without touching the database; otherwise it reads the snapshot through a one-off connection as `kh_migrator`; a missing table or function (migration `0006` not applied yet) counts as a missing snapshot.

### 3.7 Hub keys

Format `khk_<id>.<secret>`: `<id>` ten characters of `[a-z2-7]` (50 random bits, base32 without padding), `<secret>` 43 characters of unpadded urlsafe base64 of 32 random bytes. Stored: `id` and `sha256(secret as ASCII)` (a 256-bit random secret needs no slow hash). A request is checked by `kh.api_key_get(id)` then `hmac.compare_digest`. A key resolves to `Caller(sub="key:<id>", principal_type="n8n_workflow", email=None, audiences=frozenset({group}), tools=frozenset(tools))`, and `verify_key` returns `None` (a 401) when `config.is_active(group)` is false; `last_used_at` is written at most once a minute (`kh.api_key_touch`).

CLI (`auth/keys_cli.py`, connects as `kh_migrator`, loads `kh.yaml` for groups):

- Keys are created through `async def create_key(conn, *, group, tools, name, created_by, expires_days=None) -> str` in `auth/keys.py`; the CLI and the tests use it.
- `kh key create --group G --tools t1,t2 --name N [--expires-days D] [--out FILE]` (`D` 1 to 3650) prints three lines: `created key <id> for group <G>`, `key: khk_<id>.<secret>`, `shown once: store it as a secret`; with `--out FILE` the key line goes to FILE (mode 0600) instead and stdout prints `key written to <FILE>`. Errors, exit 1: `key: group <G> is not an active group`, `key: unknown tool <t>`, `key: tool <t> cannot be given to a key` (for `list_audiences` and `move_item`), `key: name <N> is not valid`. `--tools` default `search,ingest_record`. `created_by` is `cli:<getpass.getuser()>`.
- `kh key list [--json]` prints tab-separated rows under the header `id\tname\tgroup\ttools\texpires\tlast_used\tstatus`, status `active`, `expired` or `revoked`; never a secret or hash.
- `kh key revoke ID` sets `revoked_at`; `key: <id> not found` (exit 1) or `revoked <id>`.
- The tool names are the registered tools of `register_tools`; the CLI takes them from a constant `KEY_TOOLS` in `auth/keys.py` that a unit test compares with the registered names minus the two refused ones.

### 3.8 Migration `0006_identity.sql`

All tables: `ENABLE` and `FORCE ROW LEVEL SECURITY` and a policy `kh_migrator_all ... FOR ALL TO kh_migrator USING (true) WITH CHECK (true)`; `kh_app` and `kh_indexer` get no table grant and work only through `SECURITY DEFINER SET search_path = kh, pg_temp` functions (the pattern of `kh.claim_seen`), each with `REVOKE EXECUTE ... FROM PUBLIC` and an explicit `GRANT`.

```sql
CREATE TABLE kh.api_keys (
    id text PRIMARY KEY CHECK (id ~ '^[a-z2-7]{10}$'),
    secret_sha256 bytea NOT NULL CHECK (octet_length(secret_sha256) = 32),
    name text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$'),
    audience text NOT NULL,
    tools text[] NOT NULL CHECK (cardinality(tools) > 0),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz, revoked_at timestamptz, last_used_at timestamptz);
CREATE TABLE kh.oauth_clients (client_id text PRIMARY KEY, info jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz NOT NULL DEFAULT now(),
    authorized_at timestamptz);
CREATE TABLE kh.oauth_pending (state_sha256 bytea PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('upstream','consent')),
    client_id text NOT NULL, params jsonb NOT NULL,
    nonce text NOT NULL, verifier text NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_codes (code_sha256 bytea PRIMARY KEY, client_id text NOT NULL, email text NOT NULL,
    params jsonb NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_tokens (token_sha256 bytea PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('access','refresh')),
    family_id uuid NOT NULL, family_started_at timestamptz NOT NULL, client_id text NOT NULL, email text NOT NULL,
    scopes text[] NOT NULL, resource text NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz);
CREATE INDEX oauth_tokens_family ON kh.oauth_tokens (family_id);
CREATE TABLE kh.oauth_consents (email text NOT NULL, client_id text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (email, client_id));
CREATE TABLE kh.directory_snapshot (id int PRIMARY KEY CHECK (id = 1), data jsonb NOT NULL, fetched_at timestamptz NOT NULL);
```

Functions (`kh_app` unless noted; every function that takes a hash takes `bytea`):

| Function | Behaviour |
|---|---|
| `api_key_get(p_id text) RETURNS TABLE(secret_sha256 bytea, audience text, tools text[])` | the row if not revoked and not expired |
| `api_key_touch(p_id text) RETURNS void` | `last_used_at = now()` only when null or older than one minute |
| `oauth_client_get(p_id text) RETURNS jsonb` | `info` (writes nothing) |
| `oauth_client_put(p_id text, p_info jsonb, p_max integer) RETURNS boolean` | deletes clients never authorized and older than one hour, then inserts and returns true when fewer than `p_max` clients remain (authorized ones plus younger unauthorized ones); otherwise inserts nothing and returns false |
| `oauth_client_authorize(p_id text) RETURNS void`, `oauth_client_touch(p_id text) RETURNS void` | set `authorized_at` when null (a code was issued); set `last_used_at = now()` (a token was issued or refreshed) |
| `oauth_pending_put(p_state bytea, p_kind text, p_client text, p_params jsonb, p_nonce text, p_verifier text, p_expires timestamptz) RETURNS void` | deletes expired pending rows, then inserts |
| `oauth_pending_take(p_state bytea, p_kind text) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)` | `DELETE ... RETURNING` where the kind matches and not expired |
| `oauth_pending_peek(p_state bytea, p_kind text) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)` | the same row as `take` without deleting it (the consent page) |
| `oauth_code_put(p_code bytea, p_client text, p_email text, p_params jsonb, p_expires timestamptz) RETURNS void`, `oauth_code_take(p_code bytea) RETURNS TABLE(client_id text, email text, params jsonb)` | insert; `DELETE ... RETURNING` where not expired (single use) |
| `oauth_token_put(p_token bytea, p_kind text, p_family uuid, p_family_started timestamptz, p_client text, p_email text, p_scopes text[], p_resource text, p_expires timestamptz) RETURNS void` | insert |
| `oauth_token_get(p_token bytea, p_kind text) RETURNS TABLE(family_id uuid, family_started_at timestamptz, client_id text, email text, scopes text[], resource text, expires_at timestamptz, used_at timestamptz)` | the row if not revoked and not expired |
| `oauth_refresh_use(p_token bytea, p_grace_seconds int) RETURNS text` | takes `FOR UPDATE`; returns `ok` and sets `used_at` for an unused refresh token; `reuse_grace` when `used_at` is within the grace window (nothing changed); `reuse` when older (the whole family is revoked); `gone` when missing, expired or revoked |
| `oauth_token_revoke(p_token bytea) RETURNS void`, `oauth_family_revoke(p_family uuid) RETURNS void` | both set `revoked_at` on the whole family (the token's, whichever kind; the given id) |
| `oauth_consent_has(p_email text, p_client text) RETURNS boolean`, `oauth_consent_put(p_email text, p_client text) RETURNS void` | |
| `directory_snapshot_get() RETURNS TABLE(data jsonb, fetched_at timestamptz)` | `kh_app`, `kh_indexer`, `kh_migrator` |
| `directory_snapshot_put(p_data jsonb) RETURNS void` | `kh_indexer` only; upsert `id = 1`, `fetched_at = now()` |
| `oauth_purge() RETURNS integer` | `kh_indexer` only; deletes expired pending and code rows, token rows more than a day past expiry, clients never authorized after one hour and clients unused for 30 days; returns the row count |

The `kh daily` command calls `oauth_purge()` through `jobs/daily.py` `purge_identity(indexer_pool) -> int`.

### 3.9 OAuth server (`src/knowledge_hub/oauth/`)

Mounted in `build_app` (not through `MCPServer(auth=...)`, which installs `RequireAuthMiddleware` and needs an `AuthenticationMiddleware` this app does not have): `create_auth_routes(provider, issuer_url=KH_PUBLIC_URL, client_registration_options=ClientRegistrationOptions(enabled=KH_OAUTH_DCR, valid_scopes=["kh:read","kh:write"], default_scopes=["kh:read","kh:write"]), revocation_options=RevocationOptions(enabled=True))` and `create_protected_resource_routes(resource_url=<KH_PUBLIC_URL>/mcp, authorization_servers=[KH_PUBLIC_URL], scopes_supported=["kh:read","kh:write"])`, plus the hub's own `GET /oauth/callback`, `GET|POST /oauth/consent`. The authorization-server metadata document is served by a route of the hub's own, mounted before the SDK's routes so it wins: it is built with the SDK's `build_metadata` and then `token_endpoint_auth_methods_supported` and `revocation_endpoint_auth_methods_supported` are set to `["none"]` (the SDK hard-codes secret methods; Claude registers as a public client). Both metadata documents are built from `AnyHttpUrl(KH_PUBLIC_URL)`, and the protected-resource document's `authorization_servers[0]` equals the other's `issuer` string exactly (the SDK adds a trailing slash to both). `offline_access` is not in `scopes_supported`. Public paths: `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, `/oauth/callback`, `/oauth/consent`.

1. **Register** (`HubAuthProvider.register_client`): `oauth_client_put(..., KH_OAUTH_MAX_CLIENTS)` returning false raises `RegistrationError("invalid_client_metadata", "too many registered clients")`; a redirect URI outside the allowlist raises `RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed")`; a `token_endpoint_auth_method` other than `none` (the SDK fills a missing one with `client_secret_post`) raises `RegistrationError("invalid_client_metadata", "token_endpoint_auth_method must be none")`; grant types other than `authorization_code` and `refresh_token` raise `RegistrationError("invalid_client_metadata", "grant_types must be authorization_code and refresh_token")`. The route is wrapped by a global limiter of `KH_OAUTH_REGISTER_PER_MIN` (429 `Retry-After`). The consent page shows `client_name` cut to 100 characters and never fetches or renders `logo_uri` or `client_uri`.
2. **Authorize**: the redirect URI must still match the allowlist; `params.resource` must equal `<KH_PUBLIC_URL>/mcp` (a missing `resource` is accepted and bound to it), otherwise `AuthorizeError("invalid_target", "resource must be <resource_url>")`; a missing scope list becomes `["kh:read", "kh:write"]`, stored explicitly. The provider stores a pending row of kind `upstream` whose `params` are exactly `{"redirect_uri", "redirect_uri_provided_explicitly", "state", "scopes", "code_challenge", "resource"}` (the client's request), with the hash of a random `state`, a random `nonce`, a PKCE verifier and a 10-minute expiry, and returns the upstream authorization URL built from discovery (`response_type=code`, `scope=openid email`, `state`, `nonce`, `code_challenge` S256, `redirect_uri=<KH_PUBLIC_URL>/oauth/callback`, `hd` when `KH_OIDC_HOSTED_DOMAINS` has one entry, `prompt=select_account`).
3. **Callback** (`/oauth/callback`, `AuthorizationFlow.handle_callback(code, state, error)`): takes the pending row of kind `upstream` (single use); an unknown, used or expired state, or a missing `state`, raises `FlowError` (the route answers 400 `invalid request` and sets no cookie). An upstream `error`, a token-exchange failure, any `UpstreamError`, a person with no group (`Membership.audiences_for_email(email)` empty) or a redirect URI that no longer matches the allowlist ends in a redirect to the client's `redirect_uri` with `error=access_denied` and the client's `state`, and no description; only the `UpstreamError.code` is logged. With a stored consent for (email, client) it issues the code (`issue_code`, below) at once. Otherwise it stores a pending row of kind `consent` (`client_id` the client's, `nonce` and `verifier` empty strings, `params` `{"email": ..., "request": <the stored request>, "csrf": <random>}`, a fresh random ticket, 10 minutes), the route sets the cookie `__Host-kh_flow=<ticket>` (`kh_flow` when `KH_PUBLIC_URL` is `http://`; `HttpOnly`, `Secure` when https, `SameSite=Lax`, `Path=/`) and redirects to `/oauth/consent` with no query string.
4. **Consent** (`consent_context(ticket)` and `decide_consent(ticket, csrf, allow)`, both raising `FlowError` for an unknown ticket or, for the decision, a `csrf` that differs from the stored one (`hmac.compare_digest`)): GET reads the ticket from the cookie and shows a page with the client name, the redirect host and the signed-in email, the `csrf` in a hidden field; POST needs the cookie and the field. `Allow` stores the consent, calls `oauth_client_authorize`, and issues the code; `Deny` redirects with `error=access_denied`. The page sends `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self' <scheme://host[:port] of the stored redirect_uri>; frame-ancestors 'none'` (the origin is needed because browsers enforce `form-action` on the redirect after the POST), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; every shown value is HTML-escaped. The ticket is never in a URL, so it is not in access logs.
5. **Code and tokens**: `issue_code(request, email)` stores a code row (256 random bits, hashed, single use, 60 seconds, `params` the stored request, `email`) and returns the client's `redirect_uri` with `code` and the client's `state` (built with `construct_redirect_uri`); the SDK checks PKCE, that the code belongs to the authenticating client and that `redirect_uri` is unchanged. Access tokens `kha_<43 chars>` (`KH_OAUTH_ACCESS_TTL_SECONDS`), refresh tokens `khr_<43 chars>` (`KH_OAUTH_REFRESH_TTL_DAYS`), both stored as SHA-256 with `family_id`, `family_started_at`, `client_id`, `email`, `scopes`, `resource`; a refresh token expires at `min(now + KH_OAUTH_REFRESH_TTL_DAYS, family_started_at + KH_OAUTH_REFRESH_TTL_DAYS)`, so a family ends that many days after the first sign-in and the person signs in again; every issue calls `oauth_client_touch`. Refresh rotates: `oauth_refresh_use(token, 10)`; `ok`: if the person no longer holds a group the family is revoked and the answer is `TokenError("invalid_grant")`, otherwise a new pair is minted in the same family; `reuse_grace`: `invalid_grant` and nothing else changes (a legitimate double refresh retries with the newer token); `reuse`: family revoked, `invalid_grant`; `gone`: `invalid_grant`. Revocation endpoint revokes the family.
6. **Scopes**: `kh:read` allows `search, get_doc, list_stale, list_labels, usage`; `kh:write` allows `ingest_record, mark_record_outdated, propose_doc, move_item`. A token's tool allowlist is the union of its scopes; a bearer `Caller.tools` is never `None` (no scope means no tools). `list_audiences` is never allowed to a bearer caller.
7. **Upstream ID-token validation** (`oauth/upstream.py`), every check refuses on failure and nothing is read from userinfo: discovery at `<issuer>/.well-known/openid-configuration` whose `issuer` equals `KH_OIDC_ISSUER` exactly; every discovered endpoint is `https` (`http` only in `dev` and `test`) and its host is the issuer's host, or for the Google issuer one of `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com`; no redirect is followed, each call has a 10-second timeout and a 1 MiB response cap; signature with the JWKS of the discovery document: the key is selected by `kid`, `kty` `RSA` goes with `RS256` and `kty` `EC` curve `P-256` with `ES256`, a JWK `alg` or `use` that is present must match the header `alg` and `sig`, no other algorithm is accepted (never `none` or an HMAC), the JWKS is refetched at most every 5 minutes on an unknown `kid`; `iss` equals the discovered issuer (for the Google issuer `accounts.google.com` without the scheme is accepted too); `aud` contains the client id, and a present `azp` must equal it; `exp` and `iat` present, 60 seconds of leeway; `nonce` present and equal to the stored one (`hmac.compare_digest`); `sub` non-empty; `email` present, ASCII, with exactly one `@`, and `email_verified` the boolean `true` (not a string), checked before lower-casing; the email domain is in `KH_OIDC_ALLOWED_DOMAINS`; with `KH_OIDC_HOSTED_DOMAINS` set, `hd` is present and in it. The email is then lower-cased.
8. **Bearer verification** (3.10) requires the token's `resource` to equal `<KH_PUBLIC_URL>/mcp`; a token minted for anything else is refused.

**Module contract.** `oauth/provider.py` `HubAuthProvider` implements the SDK protocol only (items 1, 2, 5, 6). `oauth/flow.py` `AuthorizationFlow(store, upstream, settings, config, membership, now)` has `async handle_callback(code, state, error) -> CallbackOutcome`, `async consent_context(ticket) -> ConsentContext`, `async decide_consent(ticket, csrf, allow) -> str` (the redirect URL) and `async issue_code(request, email) -> str`; `CallbackOutcome(redirect_url: str | None, consent_ticket: str | None, csrf: str | None)` has exactly one of `redirect_url` and `consent_ticket`; `ConsentContext(client_name, redirect_host, redirect_origin, email, csrf)`; `FlowError(Exception)` is raised for every refusal the route answers with 400. `oauth/routes.py` `build_oauth_routes(settings, provider, flow)` mounts everything of this section.

The second auth path stays rejected in the sense the design record meant: one place verifies callers (`ClaimMiddleware`), now with three credential types, and `tool_guard` is unchanged except the allowlist check.

### 3.10 Caller middleware and the tool allowlist

`ClaimMiddleware` (name kept, tests keep importing it) takes `bearer: BearerResolver | None`, `membership: MembershipView | None` and `public_url: str | None`; `verifier` may be `None`. Order per request: header `X-PrivacyFence-Claim` present: with a verifier the claim path exactly as today (bare 401 on failure, `membership` used to resolve people's groups), without one a 401 with the code `claim_not_configured`; else an `Authorization` header: anything but a well-formed `Bearer <token>` is a 401 `bearer_unknown`, otherwise `bearer.resolve(token)` runs **before the body is read**, and the body is buffered afterwards only for `kh_call`; else the dev identity when set; else 401. A bearer 401 carries `WWW-Authenticate: Bearer resource_metadata="<KH_PUBLIC_URL>/.well-known/oauth-protected-resource/mcp"` and, when a token was sent, `, error="invalid_token"`; with no `public_url` the header is `Bearer error="invalid_token"` (keys only). A 401 with no credentials carries the first form when OAuth is on. `auth/bearer.py` `BearerResolver(pool, config, provider, membership, resource_url)` has `async resolve(token) -> tuple[Caller | None, str]` (the string is `""` or the rejection code `bearer_unknown` or `bearer_resource`); it is always built, `provider` and `resource_url` are `None` when OAuth is off and then `kha_` tokens resolve to `(None, "bearer_unknown")`; `khk_` tokens go to the key table, `kha_` tokens to the OAuth table. For OAuth people the audiences come from `MembershipView.current()` at the call. `Caller` gains `tools: frozenset[str] | None = None` (`None` means all); `tool_guard` refuses a tool outside it with the fixed text `this credential may not call this tool`. Rejection codes `bearer_unknown`, `bearer_resource` and `claim_not_configured` are added to `metrics.REJECTION_CODES`. A bearer caller is not bound to the body hash, so `kh_call` is parsed from the body only for the consistency check. With `KH_PUBLIC_URL` set, a small ASGI guard (`auth/hostguard.py`) answers 404 to any request whose `Host` is the public host and whose path is not one of the public paths of 3.9, so the claim path, `/webhooks/forgejo`, `/metrics` and `/healthz` are not served on the public name even if the proxy forwards them.

### 3.11 The PrivacyFence path and its limits

While `KH_PF_*` is set the claim path, the `service` principal and `list_audiences` work as today; `resolve_audiences` and `list_audiences` answer from `MembershipView.current()` so a person whose groups come only from the directory keeps them in PrivacyFence. With `legacy_privacyfence: true` the table limits apply to the static table at load and to the effective table when a snapshot is adopted (`sync`, and `MembershipCache.refresh` logs and keeps the previous membership if the new one breaks them). They can be dropped for a deployment now by setting `legacy_privacyfence: false`; the code goes in the PrivacyFence follow-up plan. Existing PrivacyFence-minted n8n tokens keep working until each workflow is moved to a hub key (3.15).

### 3.12 Forgejo

Teams and repos stay driven by `kh setup apply` from the effective membership: `ForgejoStep` takes a `membership_loader` (default: `Membership.from_config`) and reads the group's logins from the loaded membership instead of `gc.members` and `gc.approvers`. A directory-sourced login whose Forgejo account does not exist yet prints `i account <login>: no Forgejo account yet; not added to its teams` and is left out of the teams until it exists (a static login keeps `! account <login>: does not exist`). Before a directory-sourced login is added to a team the step reads the account's email through the admin API and requires it to equal the directory email (lower-case); otherwise it prints `! account <login>: email differs from directory; not added`, so an old account of someone else with the same login name is never given access. A missing or stale snapshot with a provider configured prints `! directory: snapshot missing` or `! directory: snapshot is <n>s old (limit <m>s)`; the daily drift check fails on it, which is the alert. Forgejo sign-in still goes through PrivacyFence's OIDC until a separate compose change.

### 3.13 Revocation

| Event | Takes effect |
|---|---|
| Hub key revoked or expired | the next request (lookup per request) |
| OAuth token revoked, or its refresh family revoked | the next request |
| Person removed from a Google or Keycloak group, or suspended or disabled | after the next sync (`refresh_seconds`, 300 s) plus `KH_DIRECTORY_VIEW_SECONDS` (30 s): at most about 6 minutes; `kh directory sync --once` forces it |
| Person loses every group | next refresh fails with `invalid_grant` (the access token already reads nothing) |
| Directory unreachable | last good snapshot used until `max_stale_seconds` (6 h), then directory-derived groups disappear |
| Forgejo team membership and merge rights | the next `kh setup apply --only forgejo` (hand-off: a 15-minute timer) after the sync: up to about 21 minutes |

### 3.14 Deployment and secrets (hand-off, `docs/plans/58/compose-handoff.md`)

The hub needs a public HTTPS name, egress to the upstream IdP, a second service for the sync, and its secrets as files: the OIDC client secret, the Google service account key or the Keycloak client secret. Nothing secret enters `kh.yaml` or the repository. Only the paths listed in 3.9 are public; `/webhooks/forgejo`, `/metrics`, `/healthz` stay internal; `KH_ALLOWED_HOSTS` gets the public host. The proxy must also strip the `X-PrivacyFence-Claim` header on the public name, and the hub's own guard (3.10) answers 404 for any other path. The sync reaches the Keycloak admin API only over the internal admin network, never through the public edge, and the `knowledge-hub` container's egress is limited to the issuer's hosts (3.9 item 7).

### 3.15 Migration and rollback

1. Deploy the new image with nothing configured: no new behaviour; `kh migrate` (in compose: the postgres step of `kh setup apply`) adds `0006`. The previous image refuses a database with an unknown applied migration (`db/migrate.py`), so rollback is `deploy/rollback-0006.sql` (drops every object of `0006` and deletes its row from the migrations table), then the previous image.
2. Directory: set `directory` and the per-group references, run `kh directory sync --once` and `kh directory plan`; compare `kh directory people --json` with the people the compose role rendered; then run the sync service. Rollback: `directory.provider: none` (static people again; the snapshot is ignored). Keep one static approver per group so an approve team is never empty after the rollback.
3. n8n: for each workflow create a key with the group and tools its PrivacyFence token had, change the workflow's credential to a bearer header, check one run, then revoke the PrivacyFence token in PrivacyFence. Rollback per workflow: put the old token back until it is revoked.
4. Claude: add the hub URL as a custom connector, sign in, test, then drop the PrivacyFence connector. Rollback: re-add the PrivacyFence connector (the claim path is unchanged).
5. Later (separate plan): PrivacyFence retires `audiences_tool`; the hub then sets `legacy_privacyfence: false` and unsets `KH_PF_*`.

### 3.16 Tests

Unit: rules (every skip reason, collisions, approver not a member, archived groups, static merge, limit check), config shape, key format and hashing, the upstream ID-token matrix, redirect allowlist, middleware paths, Google and Keycloak providers on `httpx.MockTransport` fixtures. Integration (Postgres): migration `0006` and RLS (no `kh_app` table access), definer functions, snapshot store and cache, sync with a fake provider, token store, refresh rotation and reuse, `kh setup` with a snapshot. Whole flow (`tests/integration/test_oauth_login_to_group_flow.py`): fake IdP (`tests/support/fake_idp.py`) plus a fake directory provider plus the ASGI app: register, authorize, sign in, consent, token, `/mcp` search that sees only the directory-derived group, a removal in the fake directory followed by a sync and cache refresh that hides it, refresh refused after the person loses every group, a key call, and a PrivacyFence claim call. Timing is tested by backdating rows or by injected `now` and `sleep` arguments, never by sleeping. `deploy/rollback-0006.sql` is tested by applying it to a migrated database and migrating again. Keycloak live provider test (`tests/integration/test_directory_keycloak_live.py`) skipped unless `KH_TEST_KEYCLOAK_URL` is set; the CI job that sets it is a protected change (the implement orchestrator reports it, Risks).

### 3.17 Security review notes

Token validation: 3.9 item 7 is the checklist (issuer, audience, nonce, `email_verified`, hosted domain, algorithms) and `tests/unit/test_oauth_upstream.py` has one test per refusal. Service-account scope: 3.5 (two read-only scopes, dedicated account, custom role). Residuals added to `docs/design.md`: a caller holding two groups can write into the wider one and the PrivacyFence popup no longer guards OAuth people; a person who is suspended in Google keeps a token until the next sync; registered clients are anonymous public clients; the consent page is the only phishing guard; delegation lets the Google key impersonate any user of the domain (the two scopes are the limit); `kh serve` holds the `kh_indexer` password, so a compromised hub could write the snapshot (a separate `kh_directory` role is a follow-up), and `kh_app` can mint codes and tokens through the definer functions; the owners and managers of a Google group decide who gets access and approver rights; a reassigned email inherits a person's groups, bounded by the family lifetime of 3.9 item 5.

## 4. Manual steps

No phase needs a real Google or Keycloak: every test uses a fake or a recorded response, and the live Keycloak test is skipped unless `KH_TEST_KEYCLOAK_URL` is set. So nothing is needed **before implementation**. Steps 1 to 4 below can be done while the phases run, so the real systems are ready for step 5 at the end. Nothing here is a secret in chat: each secret goes into the named file on the server or the named place, never into the plan, an issue or an ask. The compose repository's own docs are `https://github.com/andras-tkcs/tkcs-work-compose/blob/main/docs/keycloak.md` and `https://github.com/andras-tkcs/tkcs-work-compose/blob/main/docs/knowledge-hub.md`; the hand-off for the changes the compose repository needs is `https://github.com/tkcs-lab/knowledge-hub/blob/claude/oidc-group-access-plan-yvtt1l/docs/plans/58/compose-handoff.md`.

**Before implementation:** none.

### After implementation

**ma1-keycloak-test-realm: the Keycloak test clients and groups (about 20 minutes).** Needs the realm `tkcs` (or a separate test realm) and the admin console over the tailnet (`https://<keycloak_admin_tailnet_host>:8443`, see `docs/keycloak.md` in the compose repository, "Admin console").

1. Open the console, pick the realm, **Clients**, **Create client**: Client type `OpenID Connect`, Client ID `kh-hub`. You see the capability page.
2. Turn **Client authentication** on and keep **Standard flow** on; turn everything else off. Next.
3. Valid redirect URIs: `https://<public hub host>/oauth/callback`. Save. You see the client settings.
4. **Advanced** tab, **Proof Key for Code Exchange Code Challenge Method**: `S256`. Save.
5. **Credentials** tab: copy the client secret into the server file named in the hand-off (`kh_oidc_client_secret`, mode 0400). Do not paste it anywhere else.
6. **Clients**, **Create client**: Client ID `kh-directory`, **Client authentication** on, only **Service accounts roles** on, everything else off. Save.
7. In `kh-directory`, **Service accounts roles** tab, **Assign role**, filter by clients, select `realm-management` roles `view-users`, `query-users` and `query-groups`. You see all three listed.
8. **Credentials** tab: copy the secret into the server file `kh_directory_keycloak_secret`.
9. **Realm hardening** (Realm settings, Login): user registration off, **Edit username** and email editing by users off, **Verify email** on; under Identity providers keep automatic account linking by email off. The hub trusts `email_verified` from this realm.
10. **Groups**: make sure `kh-all` and `kh-all-approvers` exist (the compose role creates `kh-<group>` and `kh-<group>-approvers`); add one test user to each. No `groups` claim mapper is needed: the hub reads groups from the admin API, not from tokens.
Report back: pass/fail for "a test user is in `kh-all` and the two clients exist".

**ma2-google-oauth-client: the Google OAuth client (about 15 minutes).** Needs a Google Cloud project owned by the Workspace organisation.

1. Open `https://console.cloud.google.com/apis/credentials`, pick the project.
2. Configure the consent screen first if the page asks (user type **Internal**, app name `Knowledge hub`, support address, your domain).
3. **Create credentials**, **OAuth client ID**, application type **Web application**, name `knowledge-hub`.
4. Authorized redirect URI: `https://<public hub host>/oauth/callback`. Create. You see a client ID and secret.
5. Put the client ID into `KH_OIDC_CLIENT_ID` (a variable, not secret) and the secret into the server file `kh_oidc_client_secret`.
6. Set `KH_OIDC_ISSUER=https://accounts.google.com`, `KH_OIDC_HOSTED_DOMAINS=<your domain, or a comma list of every domain of the organisation>` and `KH_OIDC_ALLOWED_DOMAINS=<your domain>`.
Report back: pass/fail for "the client exists and the redirect URI matches the hub".

**ma3-google-service-account: the service account and domain-wide delegation (about 30 minutes).** Needs a Workspace super administrator.

1. Enable the Admin SDK API at `https://console.cloud.google.com/apis/library/admin.googleapis.com` in the project.
2. `https://console.cloud.google.com/iam-admin/serviceaccounts`, **Create service account** `kh-directory`, no project roles. Open it, **Keys**, **Add key**, JSON: a file downloads. Move it to the server file `kh_directory_google_key` (mode 0400) and delete the download.
3. Copy the service account's **Unique ID** (the numeric client ID).
4. `https://admin.google.com/ac/users`: create a dedicated user `kh-directory-reader@<your domain>` (no mailbox use, a random password nobody keeps, 2-step verification enforced, no licence and no other role).
5. `https://admin.google.com/ac/roles`: **Create new role** `kh-directory-read` with exactly the privileges **Admin console privileges, Users, Read** and **Groups, Read**. Assign it to `kh-directory-reader@<your domain>` (**Assign admin** on that role). You see the user listed under the role.
6. `https://admin.google.com/ac/owl/domainwidedelegation`: **Add new**, Client ID the Unique ID, OAuth scopes exactly `https://www.googleapis.com/auth/admin.directory.group.member.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly`. Authorise. You see the entry.
7. Set `KH_DIRECTORY_GOOGLE_SUBJECT=kh-directory-reader@<your domain>`.
8. Set an expiry on the key if the organisation policy allows it, write the rotation date into the runbook, and from time to time check the Admin console reporting (`https://admin.google.com/ac/reporting`, OAuth log events) that tokens issued to this client name only the reader account.
Report back: pass/fail for "`kh directory sync --once` prints `directory: google <n> users`" (after step ma5 starts the service).

**ma4-google-groups: the Google groups (about 15 minutes).** Needs Groups admin rights.

1. `https://admin.google.com/ac/groups`, **Create group**: `kh-all@<your domain>` (members) and `kh-all-approvers@<your domain>` (approvers). Repeat for every kh group (for example `sensitive`).
2. Add people (or nested groups) as members. Every approver must also be in the members group, directly or through nesting; an approver who is not a member is skipped with a warning.
3. In **Access settings** keep who-can-join restricted, make only administrators owners and managers (they decide who gets hub access and who can approve), allow no external members, and nest a group only if it is itself restricted; a group that contains the whole domain is refused by the hub on purpose.
4. Put the addresses in the compose variables that render `groups.<g>.directory.google` and `directory.provider: google` into `kh.yaml` (see the hand-off; a hand edit of the rendered file is overwritten at the next converge).
Report back: pass/fail for "`kh directory plan` lists the expected people and no unexpected skip".

**ma5-deploy-and-probe: deploy the compose hand-off and probe the live system (about 1 hour).** Needs the compose changes of the hand-off applied and the hub released.

1. Converge the compose stack per the hand-off (the converge applies migration `0006` through `kh setup apply`).
2. Check `https://<public hub host>/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server` return JSON, and that `/metrics` and `/webhooks/forgejo` return 404 or are unreachable from the internet.
3. In claude.ai, **Settings**, **Connectors**, **Add custom connector** with `https://<public hub host>/mcp`. You see the Google (or Keycloak) sign-in, then the hub's consent page naming the client and your email; allow it.
4. Ask Claude to run a hub search; it returns only your groups' content. Remove yourself from the Google group, run `kh directory sync --once`, wait 30 seconds, search again: the group's content is gone.
5. In Claude Code run `claude mcp add --transport http kh https://<public hub host>/mcp` and finish the browser sign-in; run a search.
6. Check `kh setup plan` exits 0, the sync service is running, the 15-minute team timer is active and `kh-daily` is not failed.
Report back: pass/fail for each of steps 2 to 6.

**ma6-n8n-cutover: move n8n workflows to hub keys (about 15 minutes per workflow).** Needs `kh key` available in the one-off container and n8n 2.48.5 or newer.

1. For each workflow, run `kh key create --group <g> --tools <the tools its token had> --name <workflow>`; copy the key line into n8n's credential store as a header credential `Authorization: Bearer <key>` (never into chat).
2. Change the workflow's MCP client node to the new credential and run it once; you see a successful `search` or `ingest_record`.
3. In PrivacyFence **Settings, Scoped tokens**, revoke the old token.
4. Run `kh key list`; each workflow has a key and `last_used` is recent.
Report back: pass/fail per workflow, and the line "n8n's MCP client node accepts the hub's streamable HTTP endpoint" (unconfirmed in research).

## 5. Risks and open questions

1. **Claude's registration and refresh behaviour is documented, not tested here** (research r-20261119-2, medium confidence, draft spec page). If claude.ai refuses the registration response or loops on sign-in in ma5, a worker or the human sees a 400 or a redirect loop on `/authorize`; the fallback is a pre-registered client (`kh client`, not built) or CIMD. Stop condition for p11 and p13: if the SDK's `create_auth_routes` or provider protocol differs from the signatures in 3.9 (checked against `mcp==2.3.0`), stop with `status=blocked`.
2. **Google without delegation is unconfirmed.** If the organisation forbids domain-wide delegation, the Google provider needs the Cloud Identity API (paid edition, Group Administrator role on the service account) and a separate user-state call; that is a follow-up spike, not in this plan.
3. **n8n's MCP client with streamable HTTP is unconfirmed** (n8n docs list Bearer and header auth but mention SSE). ma6 settles it; there is no SSE endpoint to fall back to.
4. **`provider.authorize` contract.** The consent page runs after the upstream callback, so the code is issued by the hub's own flow (`AuthorizationFlow.issue_code`), not by the SDK's authorize handler, and the redirect is built with `construct_redirect_uri`. If the SDK's token handler needs state the hub does not store, p11 stops blocked.
5. **Sync boundary needs a compose service.** Until the hand-off lands the directory data is never fetched; `kh setup plan` then prints `! directory: snapshot missing` when a provider is configured. That is intended.
6. **Change guard.** A real mass removal, or a doubling, is refused until `--accept-change`; the sync logs the refusal at ERROR with the marker `directory-refused` every cycle and `sync --once` exits 3. Left alone it becomes a directory outage after `max_stale_seconds`, so the hand-off asks for an alert on that marker.
7. **Different `sub` per path** (3.2): cursors and the usage hash of one person change when they move from PrivacyFence to OAuth.
8. **Personal data in new tables:** `oauth_tokens`, `oauth_codes`, `oauth_consents` and the snapshot hold email addresses; `kh daily` purges expired token rows and unused clients; the snapshot is replaced each sync.
9. **Protected path.** The Keycloak CI job belongs in `.github/workflows/ci.yml`, a protected path: no phase edits it; the implement orchestrator reports it as `protected-change:` and the owner applies it. Until then the live Keycloak test is skipped.
10. **Registration is open by design** (rate limit, cap, allowlist, consent). Dynamic registration is marked deprecated in the draft spec; if Claude moves to CIMD only, a later change is needed.
11. **Login collisions** (`anna@a.org`, `anna@b.org`) skip both people; the compose role stopped the converge instead. The skip is reported by `kh directory plan`.
12. **Rollback of `0006` needs a script.** The migration runner refuses a database with a migration the image does not ship; `deploy/rollback-0006.sql` must be run before an older image is started.
13. **Shared database role.** The sync uses `kh_indexer`, which `kh serve` also holds; a separate `kh_directory` role is a follow-up (3.17).
14. **Existing tests build `Caller(...)`, `ClaimMiddleware(...)` and `KhConfig(...)` directly** (`tests/support/principals.py`); every new parameter has a default so they keep working. A worker who has to change an existing assertion stops blocked.

## Implementation manifest

```yaml
plan_slug: oidc-oauth-directory
feature_branch: claude/oidc-group-access-plan-yvtt1l
tracking_issue: 58
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-keycloak-test-realm
    title: Create the Keycloak clients kh-hub and kh-directory and the test groups
    why: CI cannot reach the real Keycloak; the live provider test and the test sign-in need it.
  - id: ma2-google-oauth-client
    title: Create the Google OAuth client for the hub sign-in
    why: Only a person with Cloud console access can create it; production sign-in needs it.
  - id: ma3-google-service-account
    title: Create the service account, the reader account and the domain-wide delegation
    why: Only a Workspace super administrator can grant delegation; the Google provider needs it.
  - id: ma4-google-groups
    title: Create the Google groups for each kh group
    why: The directory reads these groups; only Workspace admins can create them.
  - id: ma5-deploy-and-probe
    title: Apply the compose hand-off, deploy, and probe Claude and the directory live
    why: CI cannot reach Google, claude.ai or the production network.
  - id: ma6-n8n-cutover
    title: Move each n8n workflow to a hub key and revoke its PrivacyFence token
    why: Only the live n8n shows that its MCP client accepts the hub endpoint and a bearer key.
verify_after_merge:
  - .venv/bin/python -m pytest -q -k directory
  - .venv/bin/python -m pytest -q -k keys
  - .venv/bin/python -m pytest -q -k bearer
final_checks:
  - "`tld plan deviations check --file docs/plans/58/deviations.md --plan docs/plans/58/plan.md --all` exits 0"
  - "`awk '/^## \\[Unreleased\\]/{f=1;next} /^## \\[/{f=0} f' CHANGELOG.md | grep -c \"kh directory\"` prints 1 or more and the changelog has no new version heading"
  - "`grep -c \"KH_OIDC_ISSUER\" docs/configuration.md` prints a number above 0"
phases:
  - id: p1-config-settings
    title: kh.yaml directory keys, relaxed approvers rule, and the new settings
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/settings.py
      - tests/unit/test_config.py
      - tests/unit/test_settings.py
    brief: |
      Read first: plan sections 3.3 and 3.11, src/knowledge_hub/config.py, src/knowledge_hub/settings.py, tests/unit/test_config.py, tests/unit/test_settings.py.
      Set up `.venv` as README.md Quick start says. Write the tests of step 5 first and see them fail.
      1. `config.py`: add `GroupDirectoryConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`, `members: list[str] = []`, `approvers: list[str] = []`; each entry a non-empty string of at most 254 characters, no duplicates within a list (error `directory reference <x> is listed twice`). Add the field `directory: dict[str, GroupDirectoryConfig] = {}` to `GroupConfig`; its keys must match `^[a-z][a-z0-9_]{0,30}$` (error `directory provider name <k> is not valid`). Change `GroupConfig.approvers` to `list[str] = []`.
      2. `config.py`: add `DirectoryConfig(BaseModel)` (extra forbid): `provider: str = "none"` matching `^(none|[a-z][a-z0-9_]{0,30})$`; `domains: list[str] = []` (lower-cased, unique, each matching `^[a-z0-9][a-z0-9.-]{0,252}$`); `refresh_seconds: int = 300` (60 to 3600); `max_stale_seconds: int = 21600` (at most 604800 and at least twice `refresh_seconds`, error `directory.max_stale_seconds must be at least twice refresh_seconds`); `shrink_guard_percent: int = 50` (1 to 99). A model validator refuses `provider != "none"` with empty `domains` (error `directory.domains is required when directory.provider is set`). Add to `KhConfig`: `directory: DirectoryConfig = DirectoryConfig()` and `legacy_privacyfence: bool = True`.
      3. `config.py` `_validate`: a group with no static `approvers` is allowed only when some entry of `group.directory` has a non-empty `approvers` (otherwise keep `group <g> needs at least one approver`); the check that a static approver is a static member is skipped when some entry of `group.directory` has a non-empty `members`. Move the three audience-limit checks (200, 20000, 1,000,000 characters, same messages) into a module-level function `check_audience_limits(table: Mapping[str, Sequence[str]]) -> None` raising `ValueError`, and call it from `_validate` only when `self.legacy_privacyfence` is true. `KhConfig.audiences_for_email` and `audience_members` stay unchanged.
      4. `settings.py`: add the fields of plan 3.3 with the names `public_url`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret_file`, `oidc_allowed_domains` (str, default ""), `oidc_hosted_domains` (str, default ""), `oauth_dcr` (bool, True), `oauth_redirect_allowlist` (str, the default of 3.3), `oauth_access_ttl_seconds` (3600, 300 to 86400), `oauth_refresh_ttl_days` (30, 1 to 365), `oauth_max_clients` (200, 1 to 10000), `oauth_register_per_min` (30, 1 to 1000), `oauth_authorize_per_min` (600, 1 to 100000), `directory_view_seconds` (30, 5 to 600), `directory_google_credentials_file`, `directory_google_subject`, `directory_keycloak_url`, `directory_keycloak_realm`, `directory_keycloak_client_id`, `directory_keycloak_client_secret_file`, `directory_keycloak_subgroups` (bool, False). In `_check_env_rules`: when any of `public_url`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret_file` is set, all must be set, else `ValueError("KH_OIDC_* and KH_PUBLIC_URL must be set together")`; with OAuth on and `oidc_allowed_domains` empty, `ValueError("KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on")`; with OAuth on and the host of `oidc_issuer` equal to `accounts.google.com` and `oidc_hosted_domains` empty, `ValueError("KH_OIDC_HOSTED_DOMAINS is required for a Google issuer")`; with OAuth on and `dev_identity` set, `ValueError("KH_DEV_IDENTITY must not be set while OAuth is on")`; `public_url` must be `https://<host>[:port]` with no path, userinfo, query, fragment or trailing slash, or `http://` with host `localhost`, `127.0.0.1` or `[::1]` and `env` `dev` or `test` (error `KH_PUBLIC_URL must be an https URL without a path`). Add properties `oauth_enabled: bool`, `resource_url: str | None` (`<public_url>/mcp`), `allowed_domains_tuple`, `hosted_domains_tuple` and `redirect_allowlist_tuple` (lower-case, trimmed, empty entries dropped).
      5. Tests: extend `tests/unit/test_config.py` with a test per rule above (empty approvers with a directory approvers list loads and without it keeps the old error; `legacy_privacyfence: false` loads a table of 201 groups that the default refuses with the old message; `directory.provider: google` without domains refused; an unknown key under `directory` refused; `deploy/kh.example.yaml` still loads). Extend `tests/unit/test_settings.py` with a test per settings rule and `test_public_url_shapes` (`https://h` and `https://h:8443` accepted; `https://h/`, `https://h/x`, `https://u@h`, `https://h?q`, `http://h` with `KH_ENV=dev`, and `http://localhost` with `KH_ENV=prod` refused; `http://localhost:8080` with `KH_ENV=test` accepted).
      Stop with `status=blocked` if an existing test must change an assertion to pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_settings.py -q passes"
      - "grep -n \"def check_audience_limits\" src/knowledge_hub/config.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/config.py src/knowledge_hub/settings.py tests/unit/test_config.py tests/unit/test_settings.py passes"

  - id: p2-migration-identity
    title: Migration 0006, its rollback script and the daily purge
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0006_identity.sql
      - deploy/rollback-0006.sql
      - src/knowledge_hub/jobs/daily.py
      - tests/integration/test_migration_0006.py
      - tests/integration/test_rollback_0006.py
      - tests/integration/test_migration_0002.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_setup_postgres.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_daily.py
    brief: |
      Read first: plan section 3.8, src/knowledge_hub/migrations/0001_baseline.sql (lines 120-240, the `kh.claim_seen` pattern), src/knowledge_hub/migrations/0005_move_record.sql, src/knowledge_hub/db/migrate.py, tests/integration/test_migration_0002.py (line 100), tests/integration/test_migration_0004.py, tests/integration/test_rls_tables.py, tests/integration/test_schema_tables.py, tests/integration/test_daily.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv` as README.md Quick start says. Write the tests of step 5 first.
      1. Create `0006_identity.sql` with the seven tables of plan 3.8 exactly as written, each with `ENABLE` and `FORCE ROW LEVEL SECURITY` and a policy `kh_migrator_all ... FOR ALL TO kh_migrator USING (true) WITH CHECK (true)`.
      2. Create every function of the table in plan 3.8, each `SECURITY DEFINER`, `SET search_path = kh, pg_temp`, `REVOKE EXECUTE ... FROM PUBLIC`, then `GRANT EXECUTE` to `kh_app` for every function except `directory_snapshot_put` and `oauth_purge`; `directory_snapshot_put` and `oauth_purge` to `kh_indexer` only; `directory_snapshot_get` to `kh_app`, `kh_indexer` and `kh_migrator`. `oauth_refresh_use` follows this shape: `SELECT used_at, family_id INTO ... FROM kh.oauth_tokens WHERE token_sha256 = p_token AND kind = 'refresh' AND revoked_at IS NULL AND expires_at > now() FOR UPDATE; IF NOT FOUND THEN RETURN 'gone'; IF used_at IS NULL THEN UPDATE ... SET used_at = now() WHERE token_sha256 = p_token; RETURN 'ok'; IF used_at > now() - make_interval(secs => p_grace_seconds) THEN RETURN 'reuse_grace'; UPDATE kh.oauth_tokens SET revoked_at = now() WHERE family_id = v_family AND revoked_at IS NULL; RETURN 'reuse';`. `oauth_client_put` runs `DELETE FROM kh.oauth_clients WHERE authorized_at IS NULL AND created_at < now() - interval '1 hour'`, counts the remaining rows and inserts only when the count is below `p_max`. `oauth_pending_put` first deletes rows with `expires_at < now()`.
      3. Create `deploy/rollback-0006.sql`: `DROP FUNCTION IF EXISTS` for every function of step 2 (with its argument types), `DROP TABLE IF EXISTS ... CASCADE` for the seven tables, and a `DELETE` of the row `0006_identity.sql` from the table in which `db/migrate.py` records applied migrations (read the table name there).
      4. `jobs/daily.py`: add `async def purge_identity(indexer_pool) -> int` that runs `SELECT kh.oauth_purge()` in a transaction and returns the number. Do not change `run_daily`.
      5. Tests. `tests/integration/test_migration_0006.py` (expiry and age are tested by backdating rows with an `UPDATE` as `kh_migrator`, never by sleeping): the seven tables exist with row security forced; `test_0006_function_privileges_match_matrix` asserts `has_function_privilege` for `kh_app`, `kh_indexer`, `kh_migrator` and a fresh role against a literal expected dict for every function 0006 creates; `test_0006_functions_are_definer_with_search_path` checks `prosecdef` and `proconfig`; `kh_app` and `kh_indexer` get `permission denied` on `SELECT` from each table; each function behaves as the table says (expired or revoked key not returned; `api_key_touch` writes at most once a minute and again after the row is backdated by a minute; pending and code `take` return the row once and then nothing; `oauth_pending_peek` does not delete; a state taken as kind `consent` and a ticket taken as kind `upstream` return nothing; `oauth_client_put` refuses at `p_max`, removes unauthorized clients older than an hour and keeps authorized ones; `oauth_refresh_use` returns `ok`, `reuse_grace` with a grace of 60, `reuse` with a grace of 0 (the family's other tokens are revoked), then `gone`; `test_refresh_use_concurrent_calls_yield_one_ok` runs two connections with `asyncio.gather` on one unused token with grace 60 and expects the sorted results `['ok', 'reuse_grace']`; `oauth_token_revoke` of an access token revokes the whole family; `directory_snapshot_put` is refused for `kh_app` and works for `kh_indexer`; `test_oauth_purge_boundaries`: a token 23 hours past expiry is kept and 25 hours deleted, a client unused for 29 days kept and 31 deleted, a never-authorized client 59 minutes old kept and 61 deleted, consents kept). `tests/integration/test_rollback_0006.py`: apply the rollback script to a migrated database, assert the objects are gone and the migrations table no longer lists 0006, then run `migrate(dsn)` and assert 0006 is applied again. Update the existing integration tests that list migrations, tables or policies (`test_migration_0002.py` line 100 expects a fixed list of applied names, `test_migrate_runner.py`, `test_setup_postgres.py`, `test_rls_policies.py`, `test_rls_tables.py`, `test_schema_tables.py`) so they expect the new migration and tables; extend `test_daily.py` with a `purge_identity` test.
      Stop with `status=blocked` if the SQL of plan 3.8 does not apply as written; fix only the failing statement and write the change in the deviations log.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_migration_0006.py tests/integration/test_rollback_0006.py tests/integration/test_migration_0002.py tests/integration/test_migrate_runner.py tests/integration/test_setup_postgres.py tests/integration/test_rls_policies.py tests/integration/test_rls_tables.py tests/integration/test_schema_tables.py tests/integration/test_daily.py -q passes"
      - "grep -c \"CREATE TABLE kh\\.\" src/knowledge_hub/migrations/0006_identity.sql prints 7"
      - ".venv/bin/python -m ruff check . passes"

  - id: p3-directory-core
    title: Directory model, pure rules and the provider interface
    depends_on: [p1-config-settings]
    complexity: M
    touches:
      - src/knowledge_hub/directory/__init__.py
      - src/knowledge_hub/directory/model.py
      - src/knowledge_hub/directory/rules.py
      - src/knowledge_hub/directory/provider.py
      - tests/unit/test_directory_rules.py
      - tests/unit/test_directory_provider.py
    brief: |
      Read first: plan sections 3.4 and 3.5, src/knowledge_hub/config.py (`LOGIN_RE`, `KhConfig`, `check_audience_limits`), tests/support/principals.py.
      Write the tests of step 4 first and see them fail.
      1. `directory/__init__.py` holds a docstring only. `directory/model.py`: the dataclasses and the `MembershipView` protocol of plan 3.4 (`DirectoryUser` has no default for `enabled` and `email_verified`), with `Membership.from_config(config)` (static people, members and approvers of every group, archived groups included, emails lower-case; an approver who is not a final member of the group is dropped and recorded as a `Skip` with reason `approver is not a member`), `audiences_for_email(email, is_active)` (lower-case the email, find the logins whose email matches, return the groups of `members` that contain one of them and for which `is_active(group)` is true; `None` or an empty email gives an empty set) and `audience_table(is_active)` (group to sorted, de-duplicated emails of its members, active groups only).
      2. `directory/rules.py`: `login_from_email`, `classify`, `effective` exactly as plan 3.4 says, with the reason strings `disabled`, `email not verified`, `no valid email`, `domain not allowed`, `not a valid login`, `bot or reserved login`, `login shared by several emails`, `approver is not a member`. `effective` ignores directory data for archived groups and for groups absent from `fetched`, re-classifies every user on each call (so a changed `domains` takes effect without a refetch), and never raises on user data.
      3. `directory/provider.py`: `GroupRefs`, `ProviderError`, `DirectoryProvider`, `build_provider(name, settings, *, http=None)` (an `importlib` lookup in the table `{"google": "knowledge_hub.directory.google", "keycloak": "knowledge_hub.directory.keycloak"}`; an unknown name raises `ValueError("directory: unknown provider <name>")`) and `refs_from_config(config, provider) -> dict[str, GroupRefs]` (active groups whose `directory[provider]` has at least one reference).
      4. Tests, one behaviour each: every skip reason; reason order (a disabled user with a foreign domain gets `disabled`); `login_from_email` accepts `Anna.Kiss@Example.com` as `anna.kiss`, accepts a 39-character local part and refuses 40, refuses a leading `.`, `a+b@x.org`, an empty email and an address with two `@`; `Owners@dom` gives `bot or reserved login`; a collision between two directory emails with one login skips both; a static `people` login wins over a different directory email and the directory person is skipped; static `Anna@X.org` and directory `anna@x.org` are one person, not a collision; a directory email held by a static entry under another login takes that login; the same user listed twice (nested groups) gives one member and one de-duplicated `Skip`; an approver who is not a member is skipped and not an approver; static entries survive an empty fetch; archived groups get no directory data; changing `domains` between two `effective` calls on the same fetch removes a user; `fetched=None` equals `Membership.from_config`; for the config of `tests/support/principals.py` `audience_table` equals `KH_CONFIG.audience_members()` and `audiences_for_email` equals `KH_CONFIG.audiences_for_email` for every person; `build_provider` refuses an unknown name; `refs_from_config` skips archived groups and groups without references.
      Do not import the `google` or `keycloak` modules from this phase's files. Stop with `status=blocked` if `check_audience_limits` is missing from `config.py`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_rules.py tests/unit/test_directory_provider.py -q passes"
      - "grep -rn \"keycloak\" src/knowledge_hub/directory/model.py src/knowledge_hub/directory/rules.py prints nothing"
      - ".venv/bin/python -m ruff check src/knowledge_hub/directory tests/unit/test_directory_rules.py tests/unit/test_directory_provider.py passes"

  - id: p4-hub-keys
    title: Hub keys, the kh key command and the tool allowlist
    depends_on: [p2-migration-identity]
    complexity: M
    touches:
      - src/knowledge_hub/auth/keys.py
      - src/knowledge_hub/auth/keys_cli.py
      - src/knowledge_hub/auth/claims.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_hub_keys.py
      - tests/integration/test_hub_keys_db.py
    brief: |
      Read first: plan sections 3.7 and 3.10, src/knowledge_hub/auth/claims.py (`Caller`), src/knowledge_hub/mcp/errors.py (`tool_guard`), src/knowledge_hub/admin/cli.py (how a command group is dispatched), src/knowledge_hub/cli.py, tests/integration/test_replay_cache.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first. Expiry and age are tested by backdating rows as `kh_migrator`, never by sleeping.
      1. `claims.py`: add `tools: frozenset[str] | None = None` as the last field of `Caller` (existing constructors keep working) and nothing else in this file.
      2. `mcp/errors.py`: add `TOOL_NOT_ALLOWED = "this credential may not call this tool"` and, in `tool_guard`'s wrapper right after the `isinstance(call, Call)` and tool-name checks, `if caller.tools is not None and name not in caller.tools:` record `METRICS.tool_call(name, "error", 0.0)` and `raise ToolError(TOOL_NOT_ALLOWED)`.
      3. `auth/keys.py`: `KEY_RE`, `new_key() -> tuple[str, str, bytes]` (id, full key `khk_<id>.<secret>`, `sha256` of the secret) per plan 3.7, `parse_key(text) -> tuple[str, str] | None`, `KEY_TOOLS` (the registered tool names minus `list_audiences` and `move_item`, a sorted tuple written out literally), `async def create_key(conn, *, group, tools, name, created_by, expires_days=None) -> str` (inserts the row and returns the full key), `async def verify_key(pool, config, text) -> Caller | None` (calls `kh.api_key_get`, compares with `hmac.compare_digest`, requires `config.is_active(audience)`, calls `kh.api_key_touch`, returns `Caller(sub=f"key:{id}", principal_type="n8n_workflow", email=None, audiences=frozenset({audience}), token_id=id, tools=frozenset(tools))`, otherwise `None`).
      4. `auth/keys_cli.py`: `main(argv) -> int` with the subcommands, options, outputs and errors of plan 3.7 (`create`, `list [--json]`, `revoke`), using `load_settings()`, `load_config(settings.config_file)` and a psycopg connection to `settings.dsn("migrator")`. `cli.py`: in `main`, before the argparse block, `if argv and argv[0] == "key":` import and run `knowledge_hub.auth.keys_cli.main(argv[1:])`. The module must not be imported by `app.py`.
      5. Tests: `tests/unit/test_hub_keys.py` (format regex, parse refusals, two keys differ, the stored hash is `sha256` of the secret, `KEY_TOOLS` equals the names `build_mcp(deps)` registers minus the two refused ones, `tool_guard` refuses a tool outside `Caller.tools` with the fixed text without calling the function and allows it inside, `tools=None` allows all). `tests/integration/test_hub_keys_db.py`: create then verify works; wrong secret, revoked, expired (backdated), unknown group and archived group each give `None`; `list --json` shows no secret or hash and the tab-separated list has the header of 3.7; `test_create_refusals` (parametrized: inactive group, unknown tool, `list_audiences`, `move_item`, bad name each exit 1 with the error text of 3.7); `test_create_prints_three_lines_and_secret_once`; `--out FILE` writes mode 0600 and prints `key written to <FILE>`; `revoke` of an unknown id exits 1; `last_used_at` moves once within a minute and again after a backdated minute.
      Stop with `status=blocked` if `Caller` is built positionally somewhere in `src` so the new field breaks it.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_hub_keys.py tests/integration/test_hub_keys_db.py tests/unit/test_claims.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -rn \"keys_cli\" src/knowledge_hub/app.py src/knowledge_hub/deps.py prints nothing"
      - ".venv/bin/python -m ruff check . passes"

  - id: p5-oidc-relying-party
    title: Upstream OIDC client with strict ID-token validation and a fake IdP
    depends_on: [p1-config-settings]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/__init__.py
      - src/knowledge_hub/oauth/upstream.py
      - tests/support/fake_idp.py
      - tests/unit/test_oauth_upstream.py
    brief: |
      Read first: plan section 3.9 item 7, src/knowledge_hub/auth/pf_claim_verify.py (how PyJWT is used), tests/support/claims.py, src/knowledge_hub/settings.py (the `oidc_*` fields).
      Write the tests first and see them fail.
      1. `oauth/__init__.py`: a docstring only. `oauth/upstream.py`: `UpstreamConfig` (frozen dataclass: `issuer`, `client_id`, `client_secret`, `allowed_domains: tuple[str, ...]`, `hosted_domains: tuple[str, ...]`, `redirect_uri`, `allow_http: bool`), `UpstreamError(Exception)` with a short `code`, `VerifiedIdentity` (`email`, `sub`, `hd: str | None`), and `UpstreamClient(config, http: httpx.AsyncClient, *, now=time.time)` with `async discover()` (cached for one hour), `new_pkce() -> tuple[str, str]` (a 43-character verifier and its S256 challenge), `async authorization_url(state, nonce, challenge) -> str`, `async exchange(code, verifier) -> dict` (`client_secret_post`), `async verify_id_token(id_token, nonce) -> VerifiedIdentity`. Implement every check of plan 3.9 item 7 with PyJWT, in that order: endpoint host rules, no redirects (`follow_redirects=False`), 10-second timeouts, a 1 MiB cap on discovery, JWKS and token responses; JWKS fetched with `httpx` (cached five minutes, refetched once on an unknown `kid`), the JWK picked by `kid` and bound to the header `alg` as the plan says, `jwt.decode(..., algorithms=["RS256", "ES256"], audience=client_id, issuer=<discovered issuer, plus "accounts.google.com" when it is "https://accounts.google.com">, leeway=60, options={"require": ["exp", "iat", "sub"]})`, then the explicit checks (`sub` non-empty, `nonce` present and equal by `hmac.compare_digest`, `azp` equal when present, `email` ASCII with exactly one `@` and `email_verified is True` before lower-casing, domain and `hd` lists). Never call a userinfo endpoint.
      2. `tests/support/fake_idp.py`: a function `make_fake_idp(issuer, client_id, client_secret)` returning a Starlette app with `/.well-known/openid-configuration`, `/jwks`, `/authorize` (redirects to the `redirect_uri` with a fixed `code` and the request's `state`), `/token` (checks `client_id`, `client_secret`, `code`, `code_verifier` against the stored challenge and returns an `id_token` signed with an in-process RSA key), and a `claims` dict the test edits (`email`, `email_verified`, `hd`, `aud`, `iss`, `nonce` taken from the authorize request unless overridden). It exposes `sign(claims, alg="RS256", kid=None) -> str`. Tests and the flow test of p16 get it only through a function-scoped fixture, so the `claims` dict never leaks between tests.
      3. Tests in `tests/unit/test_oauth_upstream.py`, one per refusal and one per success: a valid token; `Alice@Example.Test` comes back lower-cased; wrong `iss`; Google `accounts.google.com` accepted for the Google issuer only; wrong `aud`; several audiences with the wrong `azp`; several audiences with no `azp`; `azp` present and different with one audience; `alg` `none` (hand-built); HS256 signed with the public key bytes; RS384 refused; PS256 refused; ES256 accepted; a JWK whose `kty` does not fit the header `alg` refused; expired; `exp` missing; `iat` missing; `sub` empty; `email` missing; `email_verified` missing, `"true"` as a string and `false`; a non-ASCII email and an email with two `@` refused; email domain not allowed; `hd` different; `hd` missing while hosted domains are set; nonce missing and nonce mismatch; unknown `kid` refetches once and then fails, and a second unknown `kid` within five minutes (injected `now`) does not refetch; tampered signature; discovery whose `issuer` differs; a discovered endpoint on another host refused; an `http` endpoint refused unless `allow_http`; a response over 1 MiB refused; the authorization URL carries `response_type=code`, `scope=openid email`, `code_challenge_method=S256`, the state, nonce and, with one hosted domain, `hd`. Use `httpx.ASGITransport(app=fake_idp)`; no network.
      Stop with `status=blocked` if PyJWT cannot verify an `ES256` token from the fake IdP; use `RS256` only in the fake and say so in the deviations log.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_upstream.py -q passes"
      - "grep -n \"userinfo\" src/knowledge_hub/oauth/upstream.py prints nothing"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/support/fake_idp.py tests/unit/test_oauth_upstream.py passes"

  - id: p6-google-provider
    title: Google directory provider (Directory API, delegation, two read-only scopes)
    depends_on: [p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/directory/google.py
      - tests/support/google_fake.py
      - tests/unit/test_directory_google.py
    brief: |
      Read first: plan section 3.5 (Google), src/knowledge_hub/directory/provider.py, src/knowledge_hub/directory/model.py, src/knowledge_hub/auth/pf_claim_verify.py (PyJWT usage).
      Write the tests first and see them fail. No test may reach the network or sleep.
      1. `directory/google.py`: `make(settings, http)` returning `GoogleProvider(credentials_file, subject, http, *, now=time.time, sleep=asyncio.sleep)`. It reads the service account JSON (`client_email`, `private_key`, `token_uri`), refuses a `token_uri` other than `https://oauth2.googleapis.com/token`, signs the assertion with PyJWT `RS256` as plan 3.5 says (scopes exactly `https://www.googleapis.com/auth/admin.directory.group.member.readonly` and `https://www.googleapis.com/auth/admin.directory.user.readonly`), caches the token until five minutes before expiry, and implements `fetch(refs)` and `aclose()` with the members, users, paging, retry (waits of 1 s then 3 s through `sleep`, total deadline 60 s measured with `now`) and error rules of plan 3.5. A missing credentials file or subject raises `ProviderError("google: KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE and KH_DIRECTORY_GOOGLE_SUBJECT are required")`. Every `ProviderError` text holds the endpoint name and status code only.
      2. `tests/support/google_fake.py`: `make_google_transport(groups, users, *, fail=None) -> httpx.MockTransport` serving the token endpoint, `groups/<key>/members` (with `pageToken` paging and `includeDerivedMembership=true` required in the query) and `users` (paged), recording each request, with switches for 404, one 429, a persistent 500, a 500 on page two, a malformed page and a `CUSTOMER` row. The test RSA key and key file are generated in the test with `cryptography` and `tmp_path`.
      3. Tests: the assertion decodes (with the test public key) to the required `iss`, `sub`, `scope` (exactly the two scopes) and `aud`; a second fetch reuses the token and a fetch within five minutes of expiry refreshes it (injected `now`); two member pages; a `GROUP` row is ignored; a `CUSTOMER` row fails the whole fetch with `group <ref> contains the whole domain`; 404 raises `ProviderError`; one 429 is retried (the fake `sleep` receives 1) and succeeds; persistent 500 raises after three tries with sleeps 1 and 3; the 60-second deadline stops the retries; a 500 on member page two, on users page two and a malformed members page each raise `ProviderError` and never return a partial dict; two users pages; a member row with `status` other than `ACTIVE` is not enabled; a suspended and an archived user are not enabled; an alias address maps to its user; an own-domain member missing from `users` is not enabled; an external member keeps its own address; a wrong `token_uri` is refused; an error text contains no response body; no request carries a scope outside the two.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_google.py -q passes"
      - "grep -n \"google.auth\\|googleapiclient\" src/knowledge_hub/directory/google.py prints nothing"
      - ".venv/bin/python -m ruff check src/knowledge_hub/directory/google.py tests/support/google_fake.py tests/unit/test_directory_google.py passes"

  - id: p7-keycloak-provider
    title: Keycloak directory provider and its live test fixture
    depends_on: [p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/directory/keycloak.py
      - tests/unit/test_directory_keycloak.py
      - tests/integration/test_directory_keycloak_live.py
      - tests/support/keycloak_realm.json
      - tests/support/keycloak_expected.py
    brief: |
      Read first: plan section 3.5 (Keycloak), src/knowledge_hub/directory/provider.py, src/knowledge_hub/directory/model.py.
      Write the tests first and see them fail. No unit test may reach the network or sleep.
      1. `directory/keycloak.py`: `make(settings, http)` returning `KeycloakProvider(url, realm, client_id, client_secret, subgroups, http, *, sleep=asyncio.sleep)` and `fetch(refs)` per plan 3.5 (client-credentials token, reference validation with the two regular expressions of 3.5, group lookup by path or by exact top-level name with each path segment percent-encoded, direct members with paging of 100 and `briefRepresentation=false`, recursion into `children` only when `subgroups` is true, depth over 10 raises, retries of 429 and 5xx three times with waits 1 s then 3 s). A missing setting raises `ProviderError("keycloak: KH_DIRECTORY_KEYCLOAK_URL, _REALM, _CLIENT_ID and _CLIENT_SECRET_FILE are required")`; a URL that is not `https` raises `ProviderError("keycloak: KH_DIRECTORY_KEYCLOAK_URL must be https")` unless `KH_ENV` is `dev` or `test` or its host is a single-label name; a plain `http` URL on `keycloak.example.com` is refused, on `keycloak` it is accepted. A group that is not found, a 401, 403, 5xx after three tries, or a malformed page raises `ProviderError` (status and endpoint name only); an empty group is a valid empty list. `enabled` and `emailVerified` must be booleans in the response, a missing or non-boolean value counts as `False`.
      2. `tests/support/keycloak_realm.json`: a Keycloak realm import for the realm `kh-test` with the clients `kh-hub` (confidential, standard flow, redirect `http://localhost:8080/oauth/callback`, PKCE `S256`) and `kh-directory` (service accounts on, with `view-users`, `query-users` and `query-groups`), groups `kh-all` and `kh-all-approvers` (and a child group `kh-all/team`), and users: two enabled and verified in `kh-all`, one in `kh-all-approvers` only, one disabled, one with an unverified email, one with a foreign-domain email, one in the child group. Client secrets in the file are the literal text `test-only-not-a-secret`. `tests/support/keycloak_expected.py`: the expected members and skips derived from that file, used by both the unit and the live test.
      3. `tests/unit/test_directory_keycloak.py` with `httpx.MockTransport`: top-level lookup keeps only the result whose `path` is `/<name>`; a path reference uses `group-by-path` with encoded segments; references with `..`, a trailing slash or a character outside the expression are refused; two member pages; direct members only by default; with subgroups on, a nested group's members are included, a cycle does not loop and depth 11 raises; `enabled` and `emailVerified` carry through and a missing flag counts as `False`; a user without `email` is returned with an empty email; 403, a missing group, a 5xx on page two (never a partial result), a malformed page and a persistent 5xx (sleeps 1 and 3) each raise; a plain `http` URL with a dotted host is refused and one with the host `keycloak` is accepted; the token request uses `client_credentials`; `test_realm_fixture_matches_live_expectations` serves the members and users of the realm file through `MockTransport` and asserts, through `rules.effective`, the result of `keycloak_expected.py`.
      4. `tests/integration/test_directory_keycloak_live.py`: skipped only when `KH_TEST_KEYCLOAK_URL` is unset (reason `KH_TEST_KEYCLOAK_URL unset`); when it is set and any of `KH_TEST_KEYCLOAK_REALM`, `KH_TEST_KEYCLOAK_CLIENT_ID`, `KH_TEST_KEYCLOAK_CLIENT_SECRET` is missing the test fails; with all set it fetches `kh-all` and `kh-all-approvers` and asserts the result of `keycloak_expected.py`. Do not edit `.github/workflows/ci.yml`: report `protected-change: .github/workflows/ci.yml - add a keycloak job that starts quay.io/keycloak/keycloak:26.4.0 with tests/support/keycloak_realm.json imported and sets the four KH_TEST_KEYCLOAK_ variables` with the unified diff of the new job, as the protected-changes policy says.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_keycloak.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_directory_keycloak_live.py -q reports 1 skipped with the reason KH_TEST_KEYCLOAK_URL unset"
      - ".venv/bin/python -c \"import json; json.load(open('tests/support/keycloak_realm.json'))\" exits 0"
      - "grep -rn \"keycloak\" src/knowledge_hub/directory/model.py src/knowledge_hub/directory/rules.py src/knowledge_hub/config.py prints nothing"
      - ".venv/bin/python -m ruff check src/knowledge_hub/directory/keycloak.py tests/unit/test_directory_keycloak.py tests/integration/test_directory_keycloak_live.py tests/support/keycloak_expected.py passes"

  - id: p8-directory-store
    title: Snapshot store, membership cache, load_membership and the age gauge
    depends_on: [p2-migration-identity, p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/directory/store.py
      - src/knowledge_hub/metrics.py
      - tests/unit/test_directory_store.py
      - tests/integration/test_directory_snapshot.py
    brief: |
      Read first: plan sections 3.6 and 3.11, src/knowledge_hub/directory/model.py, rules.py, src/knowledge_hub/metrics.py, tests/integration/test_daily.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first. Age is tested with an injected `now`, never by sleeping.
      1. `directory/store.py`: `snapshot_to_fetched(data) -> dict[str, GroupFetch]` (a missing or non-boolean `enabled` or `email_verified` becomes `False`; a row of the wrong shape is skipped and logged), `fetched_to_snapshot(provider, fetched) -> dict`, `async load_snapshot(conn) -> tuple[dict, datetime] | None` (calls `kh.directory_snapshot_get()`), `async put_snapshot(conn, data)`, `class MembershipCache(pool, config, settings, now=time.time)` with `async refresh()`, `current() -> Membership` and `age_seconds() -> float | None` as plan 3.6 says, and `async load_membership(config, settings) -> tuple[Membership, float | None]` (plan 3.6). `refresh()` stores the adopted snapshot's `fetched_at`; `current()` compares `now` with it on every call; a snapshot with another `provider` than `config.directory.provider` or `version` other than 1 counts as missing; with provider `none` the cache is always static and never reads the database; `refresh()` runs `effective` and, when `config.legacy_privacyfence`, `check_audience_limits(membership.audience_table(config.is_active))`; on `ValueError` it logs and keeps the previous membership (which still ages); a database error in `refresh()` is logged and also keeps it.
      2. `metrics.py`: add a gauge `kh_directory_snapshot_age_seconds` (a setter `directory_snapshot_age(seconds)` on `Metrics`, rendered in `render` next to the existing blocks, absent until set) and call it from `MembershipCache.refresh`.
      3. Tests. `tests/unit/test_directory_store.py`: conversions round-trip; a row without `enabled` is not enabled; `load_membership` with provider `none` opens no connection (monkeypatch `psycopg.AsyncConnection.connect` to raise) and returns `(static, None)`; the gauge on a fresh `Metrics()` is absent until `refresh` and then rendered. `tests/integration/test_directory_snapshot.py`: put then load round trip as `kh_indexer`; `kh_app` can `get` but not `put`; `test_current_goes_static_after_max_stale_without_refresh` (one refresh, then `now` moved past `fetched_at + max_stale_seconds`, no refresh); `test_refresh_db_error_does_not_extend_freshness`; `test_no_row_with_provider_set_is_static`; `test_provider_none_ignores_stored_snapshot` (cache and `load_membership` both static); `test_snapshot_of_other_provider_is_treated_as_missing`; the cache keeps the previous membership when the limit check fails; `load_membership` returns `(static, None)` when the table or function is missing (run against a database migrated only to 0005) and the age when a row exists.
      The serving process may import only `store.py`, `model.py` and `rules.py` from the `directory` package.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_store.py tests/integration/test_directory_snapshot.py -q passes"
      - "grep -n \"kh_directory_snapshot_age_seconds\" src/knowledge_hub/metrics.py matches"
      - ".venv/bin/python -m ruff check . passes"

  - id: p9-directory-sync
    title: The sync process and the kh directory commands
    depends_on: [p4-hub-keys, p8-directory-store]
    complexity: M
    touches:
      - src/knowledge_hub/directory/sync.py
      - src/knowledge_hub/directory/cli.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_directory_sync.py
      - tests/integration/test_directory_sync_db.py
    brief: |
      Read first: plan section 3.6, src/knowledge_hub/directory/store.py, provider.py, rules.py, src/knowledge_hub/jobs/daily.py (`purge_identity`), src/knowledge_hub/cli.py, tests/integration/test_daily.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first.
      1. `directory/sync.py`: `async sync_once(config, settings, provider, pool, *, accept_change=False) -> SyncResult` (fields `users`, `groups`, `skips`) and `async run_every(config, settings, provider, pool, *, interval: float, accept_change: bool, sleep=asyncio.sleep)`. Follow plan 3.6 exactly: refs from `refs_from_config`, one fetch that must return every requested group (a result that lacks one is a `ProviderError` and nothing is stored), the snapshot built, the audience limits checked when `legacy_privacyfence`, the change guard (`directory: refusing snapshot: <a> users changed to <b>; rerun with --accept-change`, raised as `ChangeRefused`), the store, then the log lines of 3.6. A `ProviderError` leaves the previous snapshot and is re-raised by `sync_once`; `run_every` logs provider errors and refusals (the refusal at ERROR with the marker `directory-refused`) and waits `interval`.
      2. `directory/cli.py`: `main(argv) -> int` with `sync [--once | --every N] [--accept-change]` (`N` 60 to 3600, default `config.directory.refresh_seconds`), `plan`, `people [--json]`, `show`, with the outputs of plan 3.6. `sync` connects as `kh_indexer` (`settings.dsn("indexer")`); the others read through `load_membership`. Exit 2 on a settings or config error or an unknown provider (print `directory: unknown provider <x>`), 1 on a provider error with `directory: <provider>: <reason>`, 3 on `ChangeRefused`. `src/knowledge_hub/cli.py`: dispatch `argv[0] == "directory"` to it; make `_daily` also call `purge_identity(deps.indexer_pool)` and append `; identity rows purged: <n>` to its printed line.
      3. Tests. `tests/unit/test_directory_sync.py` (a fake provider object; monkeypatch `load_membership` for the `plan`, `people` and `show` tests so they open no database): a good run stores and logs the three line kinds; a `ProviderError` keeps the old snapshot and exit code 1; `test_fetch_missing_a_requested_group_is_not_stored`; `test_change_guard_boundary` (stored 10 users, percent 50: 5 is stored, 4 is refused; stored 10 and a new 21 is refused, 20 stored); `test_first_run_without_stored_snapshot_is_not_guarded`; `--accept-change` allows the refused case; the limit check refuses an oversized table only when `legacy_privacyfence`; `plan` exits 1 with no snapshot and with a stale one; `people --json` has the keys and `source` values of 3.6; an unknown provider exits 2 with `directory: unknown provider <x>`; `run_every` waits `interval` through the injected `sleep` and logs `directory-refused`. `tests/integration/test_directory_sync_db.py`: as `kh_indexer` against the stored row, a refused change and a provider error leave `fetched_at` unchanged, and a good run moves it; `_daily` prints `identity rows purged`.
      The app must not import `directory.sync`, `directory.cli`, `directory.google` or `directory.keycloak`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_sync.py tests/integration/test_directory_sync_db.py tests/integration/test_daily.py -q passes"
      - "grep -n \"directory-refused\" src/knowledge_hub/directory/sync.py matches"
      - ".venv/bin/python -m ruff check . passes"

  - id: p10-oauth-store
    title: OAuth token helpers, the redirect allowlist and the Postgres store
    depends_on: [p2-migration-identity, p5-oidc-relying-party]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/tokens.py
      - src/knowledge_hub/oauth/store.py
      - src/knowledge_hub/oauth/allowlist.py
      - tests/integration/test_oauth_store.py
      - tests/unit/test_oauth_allowlist.py
    brief: |
      Read first: plan section 3.8, src/knowledge_hub/auth/replay.py, tests/integration/test_replay_cache.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the test first. Expiry is tested by backdating rows as `kh_migrator`.
      1. `oauth/tokens.py`: `ACCESS_PREFIX = "kha_"`, `REFRESH_PREFIX = "khr_"`, `new_token(prefix) -> str` (prefix plus 43 characters of unpadded urlsafe base64 of 32 random bytes), `token_hash(token) -> bytes` (`sha256` of the ASCII string), `new_secret() -> str` (the same 43 characters, no prefix, used for states, codes, tickets and csrf values).
      2. `oauth/store.py`: `OAuthStore(pool)` with one async method per function of plan 3.8 that `kh_app` may call: `client_get`, `client_put(client_id, info, max_clients) -> bool`, `client_authorize`, `client_touch`, `pending_put(state_hash, kind, client_id, params, nonce, verifier, expires_at)`, `pending_take(state_hash, kind)`, `pending_peek(state_hash, kind)`, `code_put`, `code_take`, `token_put(token_hash, kind, family_id, family_started, client_id, email, scopes, resource, expires_at)`, `token_get(token_hash, kind)`, `refresh_use(token_hash, grace_seconds)`, `token_revoke`, `family_revoke`, `consent_has`, `consent_put`. Row results are frozen dataclasses `PendingRow(client_id, params, nonce, verifier)`, `CodeRow(client_id, email, params)`, `TokenRow(family_id, family_started_at, client_id, email, scopes, resource, expires_at, used_at)`; a missing row is `None`. Every call is one `SELECT kh.<function>(...)` on `pool.connection()`; hashes are passed as `bytes`.
      3. `oauth/allowlist.py`: `redirect_allowed(uri: str, allowlist: Sequence[str]) -> bool` implementing exactly the algorithm of the `KH_OAUTH_REDIRECT_ALLOWLIST` row of plan 3.3 with `urllib.parse.urlsplit` (an `https` entry matches only the identical string, with no userinfo, query or fragment; a loopback entry matches scheme `http`, `hostname` equal to the entry's host, no userinfo, no fragment, any port and path; anything unparsable is refused). `tests/unit/test_oauth_allowlist.py`: `test_redirect_allowlist_refuses_lookalikes` (parametrized; each is refused): `https://claude.ai/api/mcp/auth_callback/x`, `https://claude.ai/api/mcp/auth_callback?a=1`, `https://claude.ai.evil.example/api/mcp/auth_callback`, `http://claude.ai/api/mcp/auth_callback`, `http://localhost.evil.example/cb`, `http://localhost@evil.example/cb`, `http://127.0.0.1.evil.example/`, `https://evil.example/#http://localhost`, `javascript:alert(1)`; `test_redirect_allowlist_accepts_localhost_any_port_path` (`http://localhost:1/a/b`, `http://127.0.0.1:65535/`, `https://claude.ai/api/mcp/auth_callback`).
      4. `tests/integration/test_oauth_store.py`: a test per method against the migrated test database (round trips; single use of `pending_take` and `code_take`; `pending_peek` keeps the row; a row of the other kind is not returned; `refresh_use` returns the four outcomes; `family_revoke` hides the family's access tokens; `client_put` returns false at the cap; consent round trip; `token_get` returns `None` for a row backdated past its expiry).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_oauth_store.py tests/unit/test_oauth_allowlist.py -q passes"
      - "grep -n \"sha256\" src/knowledge_hub/oauth/tokens.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/integration/test_oauth_store.py tests/unit/test_oauth_allowlist.py passes"

  - id: p11-oauth-provider
    title: HubAuthProvider, the SDK authorization-server provider
    depends_on: [p10-oauth-store, p5-oidc-relying-party, p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/provider.py
      - tests/unit/test_oauth_provider.py
      - tests/integration/test_oauth_provider_db.py
    brief: |
      Read first: plan section 3.9 (items 1, 2, 5, 6 and the module contract), `mcp.server.auth.provider` and `mcp.server.auth.handlers` of the installed `mcp==2.3.0` (`.venv/lib/python3.*/site-packages/mcp/server/auth/`), src/knowledge_hub/oauth/store.py, tokens.py, upstream.py, src/knowledge_hub/directory/model.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first. Expiry is tested by backdating rows as `kh_migrator`, never by sleeping.
      1. `oauth/provider.py`: `HubAuthProvider(store, upstream, settings, config, membership, *, now=time.time)` implementing `OAuthAuthorizationServerProvider` with the methods, errors and texts of plan 3.9 items 1, 2, 5 and 6: `get_client`, `register_client`, `authorize` (the allowlist check is `redirect_allowed` from `knowledge_hub.oauth.allowlist`), `load_authorization_code` (uses `code_take`), `exchange_authorization_code` (stores tokens with a new `family_id` and `family_started`, calls `client_touch`), `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token` (revokes the whole family). `load_access_token` returns the SDK `AccessToken` with `subject` the email, `resource` and `scopes` from the row, or `None` for an unknown, expired or revoked token. Refresh follows the four outcomes of 3.9 item 5, never extends a family past `family_started + KH_OAUTH_REFRESH_TTL_DAYS`, and revokes the family with `invalid_grant` when `membership.current().audiences_for_email(email, config.is_active)` is empty. This file has no callback or consent code.
      2. Unit tests with a fake store and a fake upstream: registration accepts `https://claude.ai/api/mcp/auth_callback`, `http://localhost:54321/cb` and `http://127.0.0.1/x` and refuses `https://evil.example/cb` and `http://localhost.evil.example/cb` with `invalid_redirect_uri` (the full lookalike matrix is in `test_oauth_allowlist.py`); a confidential client and a missing auth method refused with the texts of 3.9 item 1; wrong grant types and the cap refused; `authorize` with a wrong `resource` raises `AuthorizeError("invalid_target", ...)`, with no `resource` stores `<public_url>/mcp`, with no scopes stores both scopes; `authorize` re-checks the allowlist; the pending row has the `params` keys of 3.9 item 2.
      3. Integration tests on Postgres: a code is single use; access and refresh tokens are stored hashed (the raw token appears in no column); rotation `ok`, then a second use inside the grace gives `invalid_grant` without revoking the family, a reuse after the row is backdated past the grace revokes the family; `test_concurrent_refresh_mints_one_pair` (two concurrent refreshes of one token create exactly one new access-token row in the family); refresh for a person who lost every group gives `invalid_grant` and revokes the family; a refresh past `family_started + ttl` (backdated) gives `invalid_grant`; `test_load_access_token_none_when_expired` and `_when_revoked`; a code of client A exchanged by client B and a refresh token of A used by B are refused; `revoke_token` of an access token hides the family's refresh token.
      Stop with `status=blocked` if the SDK token handler needs the authorization code to come from `authorize` itself.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_provider.py tests/integration/test_oauth_provider_db.py -q passes"
      - "grep -n \"from knowledge_hub.oauth.allowlist import redirect_allowed\" src/knowledge_hub/oauth/provider.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/unit/test_oauth_provider.py tests/integration/test_oauth_provider_db.py passes"

  - id: p12-oauth-flow
    title: AuthorizationFlow, the upstream callback and the consent decision
    depends_on: [p10-oauth-store, p5-oidc-relying-party, p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/flow.py
      - tests/unit/test_oauth_flow.py
      - tests/integration/test_oauth_flow_db.py
    brief: |
      Read first: plan section 3.9 (items 3, 4, 5 and the module contract), src/knowledge_hub/oauth/store.py, tokens.py, upstream.py, src/knowledge_hub/directory/model.py, `mcp.server.auth.provider.construct_redirect_uri`.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first. Expiry is tested by backdating rows, never by sleeping.
      1. `oauth/flow.py`: `FlowError`, `CallbackOutcome`, `ConsentContext` (frozen dataclasses with the fields of the module contract in 3.9) and `AuthorizationFlow(store, upstream, settings, config, membership, *, now=time.time)` with `handle_callback`, `consent_context`, `decide_consent` and `issue_code` exactly as 3.9 items 3, 4 and 5 say: pending rows of kind `upstream` and `consent` with the `params` keys written there, the redirect URI check is `redirect_allowed` from `knowledge_hub.oauth.allowlist` (created by p10). Failures end as the plan says: `FlowError` for state and ticket problems; `access_denied` redirects for upstream errors, no group or a disallowed redirect URI; the csrf compared with `hmac.compare_digest`.
      2. Unit tests with a fake store and the fake IdP of `tests/support/fake_idp.py`: unknown, used and expired state and a missing state raise `FlowError`; a state used as a consent ticket and a ticket used as a state raise `FlowError`; an unverified email, a foreign domain, a wrong nonce and a token-exchange failure each redirect with `error=access_denied`, the client's `state` and no description; a person with no group redirects with `access_denied`; a person with a stored consent gets a code redirect at once; without one a consent ticket and csrf come back; `decide_consent` with a wrong csrf or a used or expired ticket raises `FlowError` and issues no code; `Allow` stores the consent and calls `client_authorize` and returns the code redirect carrying the client's `state`; `Deny` returns `error=access_denied`; the consent context holds the client name cut to 100 characters, the redirect host and origin and the email.
      3. Integration tests on Postgres: the codes stored by `issue_code` are hashed, single use and expire after 60 seconds (backdated); a ticket is single use.
      Stop with `status=blocked` if the SDK's `construct_redirect_uri` has another signature than `(base, **params)`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_flow.py tests/integration/test_oauth_flow_db.py -q passes"
      - "grep -n \"compare_digest\" src/knowledge_hub/oauth/flow.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/unit/test_oauth_flow.py tests/integration/test_oauth_flow_db.py passes"

  - id: p13-oauth-routes
    title: OAuth routes, metadata, consent page and the limiters
    depends_on: [p11-oauth-provider, p12-oauth-flow]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/routes.py
      - src/knowledge_hub/oauth/consent.py
      - tests/unit/test_oauth_routes.py
    brief: |
      Read first: plan sections 3.9 and 3.14, `mcp.server.auth.routes` of the installed `mcp==2.3.0` (`create_auth_routes`, `create_protected_resource_routes`, `build_metadata`), src/knowledge_hub/oauth/provider.py, flow.py, src/knowledge_hub/ratelimit.py.
      Set up `.venv`. Write the tests first. Use an injected `now` for the limiters.
      1. `oauth/consent.py`: `render_consent(client_name, redirect_host, email, csrf) -> str` returning a complete HTML page (title `Knowledge hub: allow access`, the three values HTML-escaped with `html.escape`, one form with the hidden field `csrf` and two buttons `decision=allow` and `decision=deny`) and `consent_headers(redirect_origin) -> dict[str, str]` returning the four headers of plan 3.9 item 4 (the CSP with the `form-action 'self' <redirect_origin>` and `frame-ancestors 'none'`, `X-Frame-Options`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`).
      2. `oauth/routes.py`: `build_oauth_routes(settings, provider, flow, *, now=time.time) -> list[BaseRoute]`: first the hub's own metadata route (plan 3.9: SDK `build_metadata` with both auth-method lists set to `["none"]`, with the registration endpoint only when `settings.oauth_dcr`), then the routes of `create_auth_routes(provider, issuer_url=AnyHttpUrl(settings.public_url), ...)` and `create_protected_resource_routes(resource_url=AnyHttpUrl(settings.resource_url), authorization_servers=[AnyHttpUrl(settings.public_url)], scopes_supported=["kh:read", "kh:write"])`, plus `GET /oauth/callback` (passes `code`, `state` and `error` to `flow.handle_callback`; answers 302 to `redirect_url`, or sets the `__Host-kh_flow` cookie (`kh_flow` over http) and answers 302 to `/oauth/consent`; `FlowError` answers 400 `invalid request` with no cookie), `GET /oauth/consent` (reads the cookie, `flow.consent_context`, renders the page with `consent_headers`; `FlowError` answers 400) and `POST /oauth/consent` (reads the cookie and the form fields `csrf` and `decision`, `flow.decide_consent`, answers 302; `FlowError` answers 400; the cookie is cleared). Hub routes accept at most 16 KiB of body (413 above). When `settings.oauth_dcr` is false the registration route is not mounted. `/register` is wrapped by a limiter of `settings.oauth_register_per_min` per minute and `/authorize`, `/oauth/callback`, `/oauth/consent` by one shared limiter of `settings.oauth_authorize_per_min` (reuse `RateLimiter` from `ratelimit.py` with fixed keys `register` and `authorize`, passing `now`); a refused call answers 429 with `Retry-After`.
      3. Tests with `httpx.ASGITransport`, stub provider and flow: the authorization-server metadata lists `issuer`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint` (only with DCR on), `code_challenge_methods_supported` containing `S256` and `token_endpoint_auth_methods_supported` equal to `["none"]`; its `issuer` string equals the protected-resource document's `authorization_servers[0]` exactly; the protected-resource document at `/.well-known/oauth-protected-resource/mcp` names `<public_url>/mcp` and both scopes; `/register` answers 429 after the limit and not before (injected clock); the shared limiter covers the three hub routes; the consent page escapes `<script>` in a client name and carries the four headers with the redirect origin in `form-action`; the cookie has `HttpOnly`, `SameSite=Lax`, `Path=/` and `Secure` over https and the `__Host-` name; a POST with a missing or different csrf is 400 and the flow is not asked to issue a code; a callback with `FlowError` is 400 and sets no cookie; an oversized POST is 413.
      Stop with `status=blocked` if `create_auth_routes` has a different signature in `mcp==2.3.0` than plan 3.9 uses.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_routes.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_oauth_routes.py -q -k headers passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/unit/test_oauth_routes.py passes"

  - id: p14-caller-wiring
    title: Bearer path in the caller middleware, app wiring, host guard and list_audiences
    depends_on: [p4-hub-keys, p8-directory-store, p13-oauth-routes]
    complexity: M
    touches:
      - src/knowledge_hub/auth/claims.py
      - src/knowledge_hub/auth/bearer.py
      - src/knowledge_hub/auth/hostguard.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/tools/audiences.py
      - src/knowledge_hub/metrics.py
      - tests/unit/test_claims.py
      - tests/unit/test_bearer_middleware.py
      - tests/unit/test_import_boundary.py
      - tests/integration/test_list_audiences.py
    brief: |
      Read first: plan sections 3.10, 3.11 and 3.2, src/knowledge_hub/auth/claims.py, src/knowledge_hub/app.py, src/knowledge_hub/deps.py, src/knowledge_hub/tools/audiences.py, tests/unit/test_claims.py, tests/integration/test_list_audiences.py, tests/unit/test_import_boundary.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first.
      1. `auth/bearer.py`: `BearerResolver(pool, config, provider, membership, resource_url)` exactly as plan 3.10 says (`async resolve(token) -> tuple[Caller | None, str]`; `khk_` to `keys.verify_key`; `kha_` to `provider.load_access_token`, whose `resource` must equal `resource_url`; people get `Caller(sub=f"u:{email}", principal_type="claude_client", email=email, audiences=membership.current().audiences_for_email(email, config.is_active), token_id=None, tools=<READ_TOOLS and WRITE_TOOLS by scope>)`). Define `READ_TOOLS` and `WRITE_TOOLS` here with the names of plan 3.9 item 6.
      2. `auth/hostguard.py`: `PublicHostGuard(app, public_host, public_paths)`, a pure ASGI wrapper that answers 404 (empty body) to an HTTP request whose `Host` header equals `public_host` and whose path is not in `public_paths` (the paths of plan 3.9; `/.well-known/` and `/oauth/` by prefix, the others exact) and passes everything else.
      3. `claims.py`: `ClaimMiddleware.__init__` gains `bearer: BearerResolver | None = None`, `membership: MembershipView | None = None` and `public_url: str | None = None`, and `verifier` may be `None`; `_Refuse` gains `headers: list[tuple[bytes, bytes]]`; `_handle` follows the order and the `WWW-Authenticate` rules of plan 3.10, including `claim_not_configured`, the non-`Bearer` `Authorization` refusal and resolving the bearer before the body is read; `_build_caller` and `_dev_caller` pass `membership` to `resolve_audiences(config, principal_type, email, claim_audiences, membership=None)`: with a `membership`, `claude_client` uses `membership.current().audiences_for_email(email, config.is_active)`; the three-argument call keeps its behaviour. Add `bearer_unknown`, `bearer_resource` and `claim_not_configured` to `metrics.REJECTION_CODES`.
      4. `mcp/server.py`: `Deps` gains `membership: Any = None`, `oauth: Any = None`, `bearer: Any = None`. `deps.py`: `make_verifier` returns `None` instead of raising when no PrivacyFence setting and no dev identity is set; `build_deps(settings, config, *, upstream_http=None)` builds `MembershipCache` always, a `BearerResolver` always (with `provider=None` and `resource_url=None` when OAuth is off), and, when `settings.oauth_enabled`, the `UpstreamClient` (using `upstream_http` or a new `httpx.AsyncClient`), `OAuthStore`, `HubAuthProvider` and `AuthorizationFlow`; it logs `auth paths: privacyfence=<on|off> oauth=<on|off> keys=on dev_identity=<on|off>`; `close_deps` closes the upstream client. `app.py`: `build_app(settings, deps, verifier)` takes `verifier: ClaimVerifier | None`; it appends `build_oauth_routes(...)` when OAuth is on and wraps the whole app in `PublicHostGuard` when `settings.public_url` is set; the lifespan builds `ClaimMiddleware` with the resolver, the membership and `public_url`, and runs a task that calls `deps.membership.refresh()` every `settings.directory_view_seconds` (the first refresh finishes before the first request is served).
      5. `tools/audiences.py`: answer from `deps.membership.current().audience_table(config.is_active)` when `deps.membership` is set, else from `config.audience_members()`.
      6. Tests: `tests/unit/test_bearer_middleware.py` with the real `BearerResolver` and a fake provider and key verifier: valid key and valid OAuth token reach the app with the right `Caller`; unknown, wrong-resource and revoked tokens give 401 with `WWW-Authenticate` carrying `resource_metadata` and `error="invalid_token"`; no credentials with OAuth on gives 401 with the bare header; with no `public_url` the header is `Bearer error="invalid_token"`; `Authorization: Basic x` gives 401 and never the dev identity; a claim header present never falls through to the bearer path (`test_invalid_claim_plus_valid_bearer_is_401`); with no verifier a claim header gives 401 `claim_not_configured`; a key works with OAuth off (`test_key_works_with_oauth_off`); the bearer is resolved before the body is read (an oversized body with a bad token is refused without buffering it); scope tests (`kh:read` only cannot call `ingest_record`, `kh:write` only cannot call `search`, no scope means no tools, `READ_TOOLS | WRITE_TOOLS` equals the registered names minus `list_audiences` and the two sets are disjoint, a refused tool's function is never called, a dispatch that differs from the body's call is refused); `PublicHostGuard` answers 404 for `/webhooks/forgejo`, `/metrics`, `/healthz` and `/mcp/../x` on the public host and passes them on another host; the lifespan test (`test_lifespan_refreshes_before_first_request_and_every_view_seconds`: a fake membership counts calls, an injected sleep, one call before the first request and N after N ticks). Parametrize the existing claim refusal tests of `tests/unit/test_claims.py` over `bearer in (None, a real BearerResolver)` with `public_url` set, asserting 401, an empty body and no `WWW-Authenticate`; a person whose group comes only from the directory snapshot appears in `tests/integration/test_list_audiences.py`'s answer and can `search` that group through a PrivacyFence claim; each new rejection code is recorded on a fresh `Metrics()`. In `tests/unit/test_import_boundary.py` add an assertion that importing `knowledge_hub.app` loads none of `knowledge_hub.directory.sync`, `.directory.cli`, `.directory.google`, `.directory.keycloak`, `knowledge_hub.auth.keys_cli`.
      Stop with `status=blocked` if an existing test must change an assertion to pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_bearer_middleware.py tests/unit/test_claims.py tests/unit/test_import_boundary.py tests/integration/test_list_audiences.py tests/integration/test_http_e2e.py -q passes"
      - "grep -n \"resource_metadata\" src/knowledge_hub/auth/claims.py matches"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"

  - id: p15-forgejo-membership
    title: kh setup and the admin commands use the effective membership
    depends_on: [p3-directory-core, p8-directory-store]
    complexity: M
    touches:
      - src/knowledge_hub/setup/forgejo.py
      - src/knowledge_hub/setup/cli.py
      - tests/unit/test_setup_membership.py
      - tests/support/forgejo_admin_fake.py
    brief: |
      Read first: plan section 3.12, src/knowledge_hub/setup/forgejo.py (lines 82-300), src/knowledge_hub/setup/cli.py, src/knowledge_hub/admin/apply.py (it builds steps through `setup/cli.py`'s `build_steps`), tests/unit/test_setup_forgejo.py, tests/support/forgejo_admin_fake.py.
      Set up `.venv`. Write the tests first. The existing tests in `tests/unit/test_setup_forgejo.py`, `test_setup_cli.py` and `test_admin_apply.py` must pass unchanged.
      1. `setup/forgejo.py`: `ForgejoStep.__init__` gains `membership_loader: Callable[[], Awaitable[tuple[Membership, float | None]]] | None = None`; with `None` the step uses `(Membership.from_config(config), None)`. A private `async _load()` runs the loader once per `plan`, `apply` and `sync_group` call and stores `self._membership` and `self._snapshot_age`. Replace every read of `gc.members` and `gc.approvers` (the `_group`, `sync_group`, `_accounts` and archived-trim code) with the group's logins from `self._membership`: `sorted(self._membership.members.get(name, ()))` and `sorted(self._membership.approvers.get(name, ()))`. A login that is in `membership.people` but not in `config.people` (directory-sourced): if its Forgejo account is missing, print `i account <login>: no Forgejo account yet; not added to its teams` and leave it out of the teams; if the account exists and its `email` (from the same `GET /users/<login>` answer) differs from the directory email (lower-case), print `! account <login>: email differs from directory; not added` and leave it out; a login from `config.people` keeps `! account <login>: does not exist`. A new method `directory_changes() -> list[Change]`, called at the end of `_sync`, prints `! directory: snapshot missing` when `config.directory.provider != "none"` and `self._snapshot_age is None`, `! directory: snapshot is <n>s old (limit <m>s)` when the age exceeds `config.directory.max_stale_seconds`, and `i directory: <n> people from the directory snapshot` otherwise; it prints nothing when the provider is `none`.
      2. `setup/cli.py`: the `forgejo` builder passes `membership_loader=lambda: load_membership(config, settings)` (imported from `knowledge_hub.directory.store`). Nothing loads the snapshot before the postgres step has run, and with provider `none` no database call is made.
      3. `tests/support/forgejo_admin_fake.py`: let the fake `GET /users/<login>` return an `email` field (settable per account) if it does not yet.
      4. Tests in `tests/unit/test_setup_membership.py`: a group with directory members gets teams containing them; directory approvers are in the approve team; a directory person without an account gets the `i` line and is not in the team; a directory person whose account email differs gets the `!` line and is not in the team; a static person without an account still gets `!`; with a provider set and no snapshot the plan prints `! directory: snapshot missing` and exits 1 (`test_setup_plan_before_0006_with_provider_prints_snapshot_missing` uses a loader that returns `(static, None)`); with a stale one the age line; with provider `none` nothing new is printed and the loader is not called against a database; `apply` before the postgres step does not load a snapshot.
      Stop with `status=blocked` if an existing assertion must change.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_membership.py tests/unit/test_setup_forgejo.py tests/unit/test_setup_cli.py tests/unit/test_admin_apply.py tests/integration/test_admin_groups.py -q passes"
      - "grep -nE \"gc\\.(members|approvers)\" src/knowledge_hub/setup/forgejo.py prints nothing"
      - ".venv/bin/python -m ruff check . passes"

  - id: p16-flow-tests
    title: Login-to-group flow and HTTP-level security tests
    depends_on: [p14-caller-wiring, p15-forgejo-membership, p9-directory-sync]
    complexity: M
    touches:
      - tests/integration/test_oauth_login_to_group_flow.py
      - tests/integration/test_oauth_http_security.py
      - tests/support/oauth_app.py
      - tests/conftest.py
    brief: |
      Read first: plan sections 3.9, 3.13 and 3.16, tests/support/fixtures_app.py, tests/support/mcp_client.py, tests/support/fake_idp.py, tests/support/seed.py, tests/conftest.py, src/knowledge_hub/app.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`.
      1. `tests/conftest.py`: add `"tests.support.oauth_app"` to `pytest_plugins` and change nothing else. `tests/support/oauth_app.py`: a fixture `oauth_stack` that builds the real app over the migrated database with OAuth on (`public_url` `https://hub.test.example`, `KH_ENV=test`), passes the fake IdP of `tests/support/fake_idp.py` (through `httpx.ASGITransport`) as `build_deps(..., upstream_http=...)`, uses a `FakeProvider` (a `DirectoryProvider` whose groups the test edits) and offers `sync()` (runs `sync_once` then `MembershipCache.refresh()`), `register_client(redirect_uri)`, `sign_in(email)` (drives `/authorize`, the fake IdP, `/oauth/callback`, `/oauth/consent` with the cookie and csrf) and `mcp_call(token, tool, args)`. Seed one document per group with `tests/support/seed.py`.
      2. `test_oauth_login_to_group_flow.py` (every test name contains `oauth`): register with `https://claude.ai/api/mcp/auth_callback`, authorize with PKCE and `resource`, sign in as `alice@example.test` with the fake IdP, consent, exchange the code, call `search` and see only the group the fake directory gave `alice`; remove `alice` from the fake directory group, `sync()`, search again and see nothing; refresh then gives `invalid_grant`; a person in the static `people` of `kh.yaml` only still signs in and reads; a skipped person (disabled in the directory) gets `access_denied`; a second sign-in of the same person and client skips the consent page; a hub key created with `create_key` reads only its group and cannot call a tool outside its list, and works with OAuth switched off; a PrivacyFence claim built with `tests/support/claims.py` still works, `list_audiences` includes the directory-only person and a `claude_client` claim for that person can `search` the directory group; a revoked key and a revoked OAuth family give 401 on the next call; `test_raw_tokens_never_logged` (caplog shows no `kha_`, `khr_` or `khk_` secret on any refusal).
      3. `test_oauth_http_security.py`: `/register` with `https://evil.example/cb` is 400 `invalid_redirect_uri`; `/authorize` with an unregistered `redirect_uri` is refused and never redirects; a token whose stored `resource` differs is 401; an access token used after `exp` (backdated) is 401; the second use of a code is `invalid_grant`; a wrong PKCE verifier is `invalid_grant`; a code exchanged by another client and a `redirect_uri` that differs at the token endpoint are `invalid_grant`; the consent POST without the cookie is 400 and no code is issued; a consent ticket cookie replayed after use is 400; the upstream callback with an unverified email, a foreign domain or a replayed state gives no code; every 401 from `/mcp` with OAuth on carries `WWW-Authenticate` with `resource_metadata`; `/webhooks/forgejo`, `/metrics` and `/healthz` on the public host are 404.
      No test reaches a real network; the only upstream is the fake IdP.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_oauth_login_to_group_flow.py tests/integration/test_oauth_http_security.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"

  - id: p17-docs-design-config
    title: design.md, configuration.md and the example kh.yaml
    depends_on: [p14-caller-wiring, p15-forgejo-membership, p6-google-provider, p7-keycloak-provider, p9-directory-sync]
    complexity: M
    touches:
      - docs/design.md
      - docs/configuration.md
      - deploy/kh.example.yaml
    brief: |
      Read first: plan sections 3.1 to 3.17, docs/design.md, docs/configuration.md, deploy/kh.example.yaml, tests/unit/test_docs_links.py, tests/unit/test_deploy_files.py.
      Keep every markdown file under 400 lines and every relative link resolving. This phase edits no other file.
      1. `docs/design.md`: rewrite "Access model" and "Claim middleware and call binding" to describe the three credential types (PrivacyFence claim, hub key, OAuth token) and groups from the effective membership (static plus directory snapshot, no restart needed for directory changes); add a section "Sign-in, OAuth and keys" (plan 3.2, 3.7, 3.9, 3.10) and a section "Directory" (3.4 to 3.6, 3.13); rewrite "Deviation from the PrivacyFence contract" to say the email is the identity from the hub's own sign-in as well; in the rejected-alternatives table replace the reason of the row "A second auth path" with `Superseded on 2026-11-19 by the owner's decision in issue #58: one middleware now verifies three credential types (design, "Sign-in, OAuth and keys").`, append to the reason of the row "The SDK's `auth=` hook" the sentence `Still true: the hub mounts the SDK's OAuth routes and verifies bearers in its own middleware.`, and add one row per rejected alternative of plan 3.1; add to "Accepted residuals" the residuals of plan 3.17 and the different-`sub` residual of 3.2.
      2. `docs/configuration.md`: add every variable of plan 3.3 to the tables (new sections "Sign-in and OAuth" and "Directory"), the new `kh.yaml` keys (`legacy_privacyfence`, `directory`, `groups.<g>.directory`) with their validation errors, the commands `kh key` and `kh directory`, and the note that `KH_PF_*` are optional; keep the sentence "The hub never holds admin credentials; `kh setup` does." true by saying the directory sync is a separate process with its own secret files.
      3. `deploy/kh.example.yaml`: add commented examples of `legacy_privacyfence: true`, a `directory:` block (`provider: google`, `domains: [example.com]`) and a `directory:` entry under group `all` with both `google` and `keycloak` references, all commented out so the file still loads and `test_example_config_loads` passes.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py tests/unit/test_deploy_files.py -q passes"
      - "grep -c \"KH_OIDC_ISSUER\" docs/configuration.md prints 1 or more"
      - "grep -n \"A second auth path.*2026-11-19\" docs/design.md matches"
      - "wc -l docs/design.md docs/configuration.md prints no file above 400 lines"

  - id: p18-docs-retire
    title: Identity runbook, README, compose example, changelog (retirement phase)
    depends_on: [p16-flow-tests, p17-docs-design-config]
    complexity: M
    touches:
      - docs/runbook-identity.md
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
      - deploy/compose.example.yaml
      - tests/unit/test_deploy_files.py
    brief: |
      Read first: plan sections 3.14, 3.15 and 4, docs/plans/58/compose-handoff.md, docs/runbook.md, README.md, deploy/compose.example.yaml, tests/unit/test_deploy_files.py, CHANGELOG.md.
      Keep every markdown file under 400 lines and every relative link resolving. This phase edits no other file.
      1. Create `docs/runbook-identity.md` (new, at most 300 lines) with the sections "Sign-in and OAuth" (what to set, the public paths, how to test with Claude and Claude Code), "Directory" (Google and Keycloak setup as the manual steps of plan section 4 without the console clicks: scopes, roles, the sync service, `kh directory plan`, the change guard and `--accept-change`, the `directory-refused` marker, the stale-snapshot alert, key rotation), "Hub keys" (create, list, revoke, rotate) and "Moving from PrivacyFence" (plan 3.15 steps 1 to 5 with the rollback of each, and `deploy/rollback-0006.sql`). In `docs/runbook.md` add a link to it and update the joiners and leavers paragraph: with a provider set, group membership changes in Google or Keycloak, directory access follows within about six minutes and Forgejo teams within about twenty-one, `kh person` and `kh group member` still edit static entries, and a restart is no longer needed for directory changes.
      2. `README.md`: replace "Agents reach it through PrivacyFence, where its tools appear as" with a sentence saying agents reach it through OAuth (Claude connectors), hub keys (n8n) or, until it is retired, PrivacyFence, where its tools appear as `kh_search` and so on; replace "a non-empty list of approvers" with "approvers (static or from the directory)"; replace "A person reads the groups that `kh.yaml` lists for their verified email" with a sentence that adds the directory; add `kh key` and `kh directory` rows to "The `kh` commands" and say in the `kh daily` row that it also purges expired OAuth rows; keep the file under 80 lines.
      3. `deploy/compose.example.yaml`: add the environment variables `KH_PUBLIC_URL`, `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID`, `KH_OIDC_ALLOWED_DOMAINS`, `KH_OIDC_HOSTED_DOMAINS` as comments with placeholder values and `KH_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_oidc_client_secret`; a second service `knowledge-hub-directory` (same image, `command: ["directory", "sync", "--every", "300"]`, networks `[db, egress]`, secrets `pg_kh_indexer` and the directory credential files, `read_only`, `cap_drop: [ALL]`), an `egress` network (not internal) and the secrets `kh_oidc_client_secret`, `kh_directory_google_key`, `kh_directory_keycloak_secret`, all as `{file: /etc/stack-secrets/<name>}`; the file must still contain no secret value. Update `tests/unit/test_deploy_files.py` so the compose example tests expect the new service, network and secrets and still check that no secret value is present.
      4. `CHANGELOG.md` under `## [Unreleased]` (no version heading): `### Added` lines for the OAuth server and consent, the upstream OIDC sign-in with `KH_PUBLIC_URL` and `KH_OIDC_*`, the directory providers `google` and `keycloak` with `kh directory sync|plan|people|show` and the `directory` keys of `kh.yaml`, hub keys with `kh key create|list|revoke`, migration `0006_identity.sql` (applied by `kh migrate` or the postgres step of `kh setup apply`; `deploy/rollback-0006.sql` undoes it before an older image is started), `legacy_privacyfence`; `### Changed` lines for `KH_PF_*` becoming optional, `groups.<g>.approvers` allowed empty with a directory approvers source, `kh setup` reading the effective membership and its new `i account` and `! directory` lines, `list_audiences` and the PrivacyFence claim path answering from the effective membership, and `kh daily` purging identity rows; one `### Security` line for the new public surface and the two Google read-only scopes.
      5. Run `.venv/bin/python -m pytest -q` and `.venv/bin/python -m ruff check .` before the last push.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py tests/unit/test_deploy_files.py -q passes"
      - "awk '/^## \\[Unreleased\\]/{f=1;next} /^## \\[/{f=0} f' CHANGELOG.md | grep -c \"kh directory\" prints 1 or more"
      - "grep -n \"knowledge-hub-directory\" deploy/compose.example.yaml matches"
      - "wc -l README.md docs/runbook.md docs/runbook-identity.md prints no file above 400 lines"
      - ".venv/bin/python -m pytest -q passes"
```
