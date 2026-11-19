# Plan 58: Hub-owned sign-in (OAuth), directory groups from Google or Keycloak, and hub keys for n8n

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator gives the hub a public URL and one upstream sign-in (Google in production, Keycloak in test), lists in `kh.yaml` which Google groups or Keycloak groups make up each kh group, and runs `kh directory sync` as a small second service. In Claude a person adds the hub URL as a connector, signs in once with Google or Keycloak, approves the client and then reads exactly the groups the directory gives them. For n8n, `kh key create --group all --tools search,ingest_record --name nightly` prints a key once; `kh key list` and `kh key revoke <id>` manage keys. Changing the provider is one line, `directory.provider`.

**Scope:** Adds an OAuth authorization server inside the hub (registration with a redirect allowlist, consent, PKCE, opaque hashed tokens), an upstream OIDC sign-in, the directory providers `google` and `keycloak` behind one interface and one pure rule module, a Postgres snapshot, hub keys, migration `0006`, and the effective membership in `kh setup`. The PrivacyFence claim path, `list_audiences` and existing PrivacyFence-minted n8n tokens keep working while `KH_PF_*` is set. Tiers, row-level security and tools are unchanged. The privacyfence and compose repositories are not touched; the compose changes are a hand-off document.

**Security:** The hub becomes internet-facing and its own token issuer. Directory credentials live only in the separate sync process. Delegation is limited to two read-only Google scopes and a dedicated reader account. PrivacyFence popups no longer guard people who connect by OAuth; OAuth scopes and the client's own tool approval replace them.

**Review focus:** the authorize, consent and redirect rules (section 3.9); the sync process boundary and fail-closed staleness (3.6); Google delegation against a direct role (3.5); coexistence with the PrivacyFence path and its table limits (3.11); the `sub` of OAuth people (3.2).

## 1. Goal

Today a person reaches the hub only through PrivacyFence, and a person's groups are whatever `kh.yaml` lists for their verified email; a Keycloak-reading Ansible role in the compose repo rewrites that list at converge time. The owner wants the hub independent of PrivacyFence: MCP clients (Claude) connect to the hub with OAuth, the hub signs people in against Google or Keycloak by OIDC, and a directory provider turns Google or Keycloak group membership into the kh groups and approvers a person holds. n8n stops using PrivacyFence-minted tokens and uses keys the hub issues, each bound to one group and a tool list. Tracking issue: https://github.com/tkcs-lab/knowledge-hub/issues/58.

## 2. Current state

- **One caller path.** `src/knowledge_hub/auth/claims.py:153-289` `ClaimMiddleware` is the only authentication: it buffers the body, verifies the `X-PrivacyFence-Claim` header with the vendored verifier (`auth/pf_claim_verify.py`, claim bound to the body hash) and refuses with a bare 401. `claude_client` groups come from `KhConfig.audiences_for_email` (`config.py:282`), `n8n_workflow` groups from the claim's audiences (`claims.py:112-121`), `service` may only call `list_audiences`. `app.py:62-79` wraps the SDK's `/mcp` endpoint in it. `deps.py:62-77` `make_verifier` raises unless `KH_PF_*` or `KH_DEV_IDENTITY` is set.
- **Groups come from `kh.yaml` only** (`config.py:57-70`, `:140-180`), read at start; `approvers` must be non-empty (`config.py:169`) and a subset of `members` (`:177`). `kh setup` builds the Forgejo teams from `gc.members` and `gc.approvers` (`setup/forgejo.py:164, 204-209, 264`) and prints `! account X: does not exist` for a login without a Forgejo account (`:278`); it never creates users.
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
| Hub key | `n8n_workflow` | `key:<id>` | none | the key's single group, only while `config.is_active(group)` |
| PrivacyFence claim | unchanged | unchanged | unchanged | unchanged, but resolved through `Membership` |

A record created through the claim path and one created by the same person through OAuth carry different `created_by_sub`; nothing compares them (checked: only the RLS insert check and audit text read it). Cursors and usage hashes of one person differ by path. This is accepted and documented.

### 3.3 Settings and `kh.yaml`

Secrets only as `*_FILE` variables. New `Settings` fields (`settings.py`), all optional:

| Variable | Default | Rule |
|---|---|---|
| `KH_PUBLIC_URL` | none | `https://host` with no path; `http://` allowed only when `KH_ENV` is `dev` or `test`. The authorization server issuer, and the resource is `<KH_PUBLIC_URL>/mcp`. |
| `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID`, `KH_OIDC_CLIENT_SECRET_FILE` | none | OAuth is on only when `KH_PUBLIC_URL` and all three are set; some but not all: `KH_OIDC_* and KH_PUBLIC_URL must be set together` (exit 2). |
| `KH_OIDC_ALLOWED_DOMAINS` | empty | Comma list of lower-case email domains; required when OAuth is on (`KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on`). |
| `KH_OIDC_HOSTED_DOMAIN` | none | When set, the ID token's `hd` must equal it (Google). |
| `KH_OAUTH_DCR` | `true` | Dynamic registration route mounted or not. |
| `KH_OAUTH_REDIRECT_ALLOWLIST` | `https://claude.ai/api/mcp/auth_callback,http://localhost,http://127.0.0.1` | Comma list. An `https` entry matches the exact URI. An `http://localhost` or `http://127.0.0.1` entry matches any port and path on that host. |
| `KH_OAUTH_ACCESS_TTL_SECONDS` | `3600` | 300 to 86400. |
| `KH_OAUTH_REFRESH_TTL_DAYS` | `30` | 1 to 365. |
| `KH_OAUTH_MAX_CLIENTS` | `200` | Registered clients; more is refused. Clients unused for 30 days are purged by `kh daily`. |
| `KH_OAUTH_REGISTER_PER_MIN` | `30` | Registration requests per minute for the whole hub. |
| `KH_DIRECTORY_VIEW_SECONDS` | `30` | How often `kh serve` re-reads the snapshot (5 to 600). |
| `KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE`, `KH_DIRECTORY_GOOGLE_SUBJECT` | none | Service account key (JSON) and the admin address it impersonates. |
| `KH_DIRECTORY_KEYCLOAK_URL`, `KH_DIRECTORY_KEYCLOAK_REALM`, `KH_DIRECTORY_KEYCLOAK_CLIENT_ID`, `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE` | none | Base URL (no trailing slash), realm, service-account client. |

`KH_PF_ISSUER`, `KH_PF_AUDIENCE`, `KH_PF_JWKS_URL` become optional with no other requirement: `make_verifier` returns `None` when they are absent, and `build_deps` logs one line `auth paths: privacyfence=<on|off> oauth=<on|off> keys=on dev_identity=<on|off>`. With every path off, only keys work and every other request is refused 401.

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
class DirectoryUser:  email: str; enabled: bool = True; email_verified: bool = True; username: str = ""
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
    def from_config(config: KhConfig) -> Membership: ...                    # static only
class MembershipView(Protocol): def current(self) -> Membership: ...
```

`rules.py` (no I/O):

- `login_from_email(email: str) -> str | None`: lower-case and trim; exactly one `@`; the local part must match `^[a-z0-9][a-z0-9_.-]{0,38}$`.
- `classify(user, domains, reserved) -> str`, first match wins, `""` means keep: `disabled` (not `enabled`), `email not verified`, `no valid email` (not exactly one `@`), `domain not allowed` (domain not in `domains`), `not a valid login`, `bot or reserved login` (login in `{config.bot_login, config.indexer_login, "owners"}`, case-folded). These are the exact reasons of the compose role, in its order.
- `effective(config, fetched: Mapping[str, GroupFetch] | None, domains: Sequence[str]) -> Membership`:
  1. Start from the static people, members and approvers.
  2. For every active group present in `fetched`, classify members then approvers. A skipped user becomes a `Skip`.
  3. Login collision: a login that more than one distinct email maps to is kept for the static email if a static `people` entry holds it (the directory email is skipped with reason `login shared by several emails`); between directory emails both are skipped with the same reason.
  4. Passing directory members join `people` and the group's members.
  5. Approvers are the static approvers plus the passing directory approvers; each must be in the group's final members, otherwise a `Skip` with reason `approver is not a member` and the person is not an approver.
  6. `skips` is sorted and de-duplicated by `(group, role, who, reason)`.
- `check_audience_limits(table)` is a function of `config.py` (the three existing messages of `config.py:181-199`, moved into one function); `sync.py` and `MembershipCache` call it, `rules.py` does not.
- `fetched is None` gives the static membership.

Archived groups take no directory data. `Membership.audiences_for_email` equals the current `KhConfig.audiences_for_email` on a static membership; the old method stays and delegates.

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

`build_provider` maps `google` and `keycloak` to `knowledge_hub.directory.google` and `.keycloak` with `importlib` (each module has `make(settings, http)`); the core never imports a provider. A fetch either returns every requested group or raises `ProviderError`; a missing group, a 4xx or 5xx after retries, a malformed page, or a refusal is never an empty list. Retry: 429 and 5xx, three tries, 1 s then 3 s, total deadline 60 s.

**Google.** Facts (research r-20261119-1, 2026-11-19): Cloud Identity `searchTransitiveMemberships` needs a paid edition and returns no suspended flag (https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveMemberships); Directory `members.list` with `includeDerivedMembership=true` covers nested members without one (https://developers.google.com/workspace/admin/directory/reference/rest/v1/members/list); `suspended` and `archived` are fields of the Directory user (https://developers.google.com/workspace/admin/directory/reference/rest/v1/users); the Directory default quota is 2,400 queries a minute per user per project (https://developers.google.com/workspace/admin/directory/v1/limits). Only the Cloud Identity API is documented to work with a role assigned to the service account directly (https://docs.cloud.google.com/identity/docs/how-to/setup); no source confirms the same for the Directory API. Decision: **domain-wide delegation** impersonating a dedicated reader account, with exactly two scopes, `https://www.googleapis.com/auth/admin.directory.group.member.readonly` and `https://www.googleapis.com/auth/admin.directory.user.readonly`. The reader account holds a custom admin role with only Groups read and Users read, and no other privilege. A direct-role variant is a follow-up spike (Risks).

- Token: sign a JWT assertion with PyJWT (`RS256`, `iss` the key file's `client_email`, `sub` `KH_DIRECTORY_GOOGLE_SUBJECT`, `scope` the two scopes space-joined, `aud` the key file's `token_uri` which must equal `https://oauth2.googleapis.com/token`, `exp` 1 h) and POST it as `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`; cache the token until five minutes before expiry. No Google client library.
- Members: for each reference, `GET https://admin.googleapis.com/admin/directory/v1/groups/<urlencoded email>/members?includeDerivedMembership=true&maxResults=200`, following `nextPageToken`. A row of type `GROUP` is ignored (its members are in the list already). A row of type `CUSTOMER` (everyone in the domain) makes the whole fetch fail with `group <ref> contains the whole domain`. A `USER` row with `status` other than `ACTIVE` is not enabled. External members come through as users and are removed by the domain rule.
- Users: one paged `GET https://admin.googleapis.com/admin/directory/v1/users?customer=my_customer&maxResults=500&fields=nextPageToken,users(primaryEmail,suspended,archived,aliases,nonEditableAliases)` per fetch. A member email is enabled when it is a primary address or alias of a user who is neither suspended nor archived; a member whose address is in a configured domain but is absent from the list is not enabled. `email_verified` is always true (Workspace-managed address).
- Group references are group email addresses; `ProviderError` on 404.

**Keycloak.** Token by client credentials at `<url>/realms/<realm>/protocol/openid-connect/token`; the client needs the realm-management roles `view-users` and `query-groups`. A reference starting with `/` is a group path (`GET /admin/realms/<realm>/group-by-path/<path>`); any other is a top-level group name (`GET /admin/realms/<realm>/groups?search=<name>&exact=true`, keeping the result whose `path` is `/<name>`); none found is a `ProviderError`. Members: `GET /admin/realms/<realm>/groups/<id>/members?briefRepresentation=false&first=<n>&max=100` until a short page; the members endpoint does not return subgroup members, so the provider also walks `GET /admin/realms/<realm>/groups/<id>/children?first=<n>&max=100` recursively (depth at most 10, a visited set) and adds each child's members. `DirectoryUser` takes `email`, `enabled`, `emailVerified`, `username`. Nothing in this module or in the core names Keycloak outside `keycloak.py`, its tests and the settings.

### 3.6 Snapshot, sync process, staleness

- **Snapshot.** One row of `kh.directory_snapshot` (3.8): `{"version": 1, "provider": "<name>", "groups": {"<g>": {"members": [{"email","enabled","email_verified","username"}, ...], "approvers": [...]}}}` holding the raw fetched users, so rule changes (domains, reserved logins) apply without a refetch. `fetched_at` is set by the database.
- **`kh directory sync [--once | --every N] [--accept-shrink]`** (`directory/sync.py`, `directory/cli.py`): connects as `kh_indexer`, builds the provider, and per run: fetch (refs from `config.groups` of active groups for the active provider; a group with no reference for the provider is left to static data); build the snapshot; `check_audience_limits` on the effective table when `legacy_privacyfence`; apply the shrink guard (refuse when the total user count of the new snapshot is below `100 - shrink_guard_percent` percent of the stored one, unless `--accept-shrink`); store with `kh.directory_snapshot_put`; log one line `directory: <provider> <n> users in <m> groups, <k> skipped`; log each skip. `--every N` (default `refresh_seconds`) loops; an error in a run is logged and the previous snapshot stays; the exit code of `--once` is 0, or 1 with `directory: <provider>: <reason>`. A refusal prints `directory: refusing snapshot: <a> users dropped to <b>; rerun with --accept-shrink`.
- **Serving.** `directory/store.py` `MembershipCache(app_pool, config, settings)`: `refresh()` reads `kh.directory_snapshot_get()`, runs `effective`, keeps the result; `current()` returns it; when `now - fetched_at > max_stale_seconds` (or there is no row while a provider is configured) `current()` returns the static membership, logs once per transition and sets the gauge `kh_directory_snapshot_age_seconds`. `kh serve` refreshes every `KH_DIRECTORY_VIEW_SECONDS`. A stale or missing snapshot therefore removes every directory-derived group (fail closed, documented as "a directory outage over `max_stale_seconds` removes directory access"); static entries and keys keep working.
- **Other commands.** `kh directory plan`: effective membership counts, every skip, and exit 1 when the snapshot is missing or stale. `kh directory people [--json]`: `[{"login","email","groups":[...],"approver_of":[...]}]` for the effective membership (the hand-off for compose's account creation). `kh directory show`: provider, age, counts. `kh daily` also runs `purge_identity` (3.8).
- **`load_membership(config, settings) -> tuple[Membership, float | None]`** (async; the membership and the snapshot age in seconds) is what `kh setup` and the admin commands use. With `directory.provider` `none` it returns the static membership and `None` without touching the database; otherwise it reads the snapshot through a one-off connection as `kh_migrator`.

### 3.7 Hub keys

Format `khk_<id>.<secret>`: `<id>` ten characters of `[a-z2-7]` (50 random bits, base32 without padding), `<secret>` 43 characters of unpadded urlsafe base64 of 32 random bytes. Stored: `id` and `sha256(secret as ASCII)` (a 256-bit random secret needs no slow hash). A request is checked by `kh.api_key_get(id)` then `hmac.compare_digest`. A key resolves to `Caller(sub="key:<id>", principal_type="n8n_workflow", email=None, audiences=frozenset({group}) if active else frozenset(), tools=frozenset(tools))`; `last_used_at` is written at most once a minute (`kh.api_key_touch`).

CLI (`auth/keys_cli.py`, connects as `kh_migrator`, loads `kh.yaml` for groups):

- `kh key create --group G --tools t1,t2 --name N [--expires-days D]` prints three lines: `created key <id> for group <G>`, `key: khk_<id>.<secret>`, `shown once: store it as a secret`. Errors, exit 1: `key: group <G> is not an active group`, `key: unknown tool <t>`, `key: tool <t> cannot be given to a key` (for `list_audiences` and `move_item`), `key: name <N> is not valid`. `--tools` default `search,ingest_record`. `created_by` is `cli:<os user>`.
- `kh key list [--json]` prints `id name group tools expires last_used status`, status `active`, `expired` or `revoked`; never a secret or hash.
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
    created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE kh.oauth_pending (state_sha256 bytea PRIMARY KEY, client_id text NOT NULL, params jsonb NOT NULL,
    nonce text NOT NULL, verifier text NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_codes (code_sha256 bytea PRIMARY KEY, client_id text NOT NULL, email text NOT NULL,
    params jsonb NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_tokens (token_sha256 bytea PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('access','refresh')),
    family_id uuid NOT NULL, client_id text NOT NULL, email text NOT NULL, scopes text[] NOT NULL,
    resource text NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz);
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
| `oauth_client_get(p_id text) RETURNS jsonb` | `info`; sets `last_used_at = now()` |
| `oauth_client_put(p_id text, p_info jsonb) RETURNS void`, `oauth_client_count() RETURNS integer` | insert; count |
| `oauth_pending_put(p_state bytea, p_client text, p_params jsonb, p_nonce text, p_verifier text, p_expires timestamptz) RETURNS void` | insert |
| `oauth_pending_take(p_state bytea) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)` | `DELETE ... RETURNING` where not expired |
| `oauth_pending_peek(p_state bytea) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)` | the same row as `take` without deleting it (the consent page) |
| `oauth_code_put(p_code bytea, p_client text, p_email text, p_params jsonb, p_expires timestamptz) RETURNS void`, `oauth_code_take(p_code bytea) RETURNS TABLE(client_id text, email text, params jsonb)` | insert; `DELETE ... RETURNING` where not expired (single use) |
| `oauth_token_put(p_token bytea, p_kind text, p_family uuid, p_client text, p_email text, p_scopes text[], p_resource text, p_expires timestamptz) RETURNS void` | insert |
| `oauth_token_get(p_token bytea, p_kind text) RETURNS TABLE(family_id uuid, client_id text, email text, scopes text[], resource text, expires_at timestamptz, used_at timestamptz)` | the row if not revoked and not expired |
| `oauth_refresh_use(p_token bytea, p_grace_seconds int) RETURNS text` | takes `FOR UPDATE`; returns `ok` and sets `used_at` for an unused refresh token; `reuse_grace` when `used_at` is within the grace window (nothing changed); `reuse` when older (the whole family is revoked); `gone` when missing, expired or revoked |
| `oauth_token_revoke(p_token bytea) RETURNS void`, `oauth_family_revoke(p_family uuid) RETURNS void` | set `revoked_at` on the token's family (refresh) or the token (access); on the family |
| `oauth_consent_has(p_email text, p_client text) RETURNS boolean`, `oauth_consent_put(p_email text, p_client text) RETURNS void` | |
| `directory_snapshot_get() RETURNS TABLE(data jsonb, fetched_at timestamptz)` | `kh_app`, `kh_indexer`, `kh_migrator` |
| `directory_snapshot_put(p_data jsonb) RETURNS void` | `kh_indexer` only; upsert `id = 1`, `fetched_at = now()` |
| `oauth_purge() RETURNS integer` | `kh_indexer` only; deletes expired pending, codes, tokens (past expiry by a day), and clients unused for 30 days; returns the row count |

The `kh daily` command calls `oauth_purge()` through `jobs/daily.py` `purge_identity(indexer_pool) -> int`.

### 3.9 OAuth server (`src/knowledge_hub/oauth/`)

Mounted in `build_app` (not through `MCPServer(auth=...)`, which installs `RequireAuthMiddleware` and needs an `AuthenticationMiddleware` this app does not have): `create_auth_routes(provider, issuer_url=KH_PUBLIC_URL, client_registration_options=ClientRegistrationOptions(enabled=KH_OAUTH_DCR, valid_scopes=["kh:read","kh:write"], default_scopes=["kh:read","kh:write"]), revocation_options=RevocationOptions(enabled=True))` and `create_protected_resource_routes(resource_url=<KH_PUBLIC_URL>/mcp, authorization_servers=[KH_PUBLIC_URL], scopes_supported=["kh:read","kh:write"])`, plus the hub's own `GET /oauth/callback`, `GET|POST /oauth/consent`. `offline_access` is not in `scopes_supported`. Public paths: `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, `/oauth/callback`, `/oauth/consent`.

1. **Register** (`HubAuthProvider.register_client`): refuses when `count >= KH_OAUTH_MAX_CLIENTS`; every redirect URI must match `KH_OAUTH_REDIRECT_ALLOWLIST` (else `RegistrationError("invalid_redirect_uri", ...)`); `token_endpoint_auth_method` must be `none`; grant types a subset of `authorization_code`, `refresh_token`. The route is wrapped by a global limiter of `KH_OAUTH_REGISTER_PER_MIN` (429 `Retry-After`).
2. **Authorize**: `params.resource` must equal `<KH_PUBLIC_URL>/mcp` (a missing `resource` is accepted and bound to it). The provider stores a pending row (hash of a random `state`, the client's request, a random `nonce`, a PKCE verifier, 10 minutes) and returns the upstream authorization URL built from discovery (`response_type=code`, `scope=openid email`, `state`, `nonce`, `code_challenge` S256, `redirect_uri=<KH_PUBLIC_URL>/oauth/callback`, `hd` when `KH_OIDC_HOSTED_DOMAIN` is set, `prompt=select_account`).
3. **Callback** (`/oauth/callback`): takes the pending row (single use), exchanges the code at the upstream token endpoint (`client_secret_post`), verifies the ID token (below), then requires `Membership.audiences_for_email(email)` to be non-empty; otherwise it redirects to the client with `error=access_denied`. With a stored consent for (email, client) it issues the code at once; otherwise it stores a second pending row (a fresh random ticket carrying the verified email) and redirects to `/oauth/consent?t=<ticket>`.
4. **Consent**: GET shows a page with the client name, the redirect host and the signed-in email and sets a `kh_csrf` cookie (random, `HttpOnly`, `Secure` unless http, `SameSite=Strict`, path `/oauth/consent`); POST needs the same value in a form field and the ticket (single use, 10 minutes). `Allow` stores the consent and issues the code; `Deny` redirects with `error=access_denied`. The page sends `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`; every shown value is HTML-escaped.
5. **Code and tokens**: the code is 256 random bits, stored hashed, single use, 60 seconds; the SDK checks PKCE. Access tokens `kha_<43 chars>` (`KH_OAUTH_ACCESS_TTL_SECONDS`), refresh tokens `khr_<43 chars>` (`KH_OAUTH_REFRESH_TTL_DAYS`), both stored as SHA-256 with `family_id`, `client_id`, `email`, `scopes`, `resource`. Refresh rotates: `oauth_refresh_use(token, 10)`; `ok`: if the person no longer holds a group the family is revoked and the answer is `TokenError("invalid_grant")`, otherwise a new pair is minted in the same family; `reuse_grace`: `invalid_grant` and nothing else changes (a legitimate double refresh retries with the newer token); `reuse`: family revoked, `invalid_grant`; `gone`: `invalid_grant`. Revocation endpoint revokes the family.
6. **Scopes**: `kh:read` allows `search, get_doc, list_stale, list_labels, usage`; `kh:write` allows `ingest_record, mark_record_outdated, propose_doc, move_item`. A token's tool allowlist is the union of its scopes. `list_audiences` is never allowed to a bearer caller.
7. **Upstream ID-token validation** (`oauth/upstream.py`), every check refuses on failure and nothing is read from userinfo: discovery at `<issuer>/.well-known/openid-configuration` whose `issuer` equals `KH_OIDC_ISSUER` exactly and whose endpoints are `https` (`http` only in `dev` and `test`); signature with the JWKS of the discovery document, algorithm in `RS256`, `ES256` only (never `none` or an HMAC), JWKS refetched at most every 5 minutes on an unknown `kid`; `iss` equals the discovered issuer; `aud` contains the client id and, with several audiences, `azp` equals it; `exp` and `iat` present, 60 seconds of leeway; `nonce` equals the stored one; `sub` non-empty; `email` present and `email_verified` the boolean `true` (not a string); the email domain is in `KH_OIDC_ALLOWED_DOMAINS`; with `KH_OIDC_HOSTED_DOMAIN`, `hd` equals it and the email domain equals `hd`. The email is lower-cased.
8. **Bearer verification** (3.10) requires the token's `resource` to equal `<KH_PUBLIC_URL>/mcp`; a token minted for anything else is refused.

The second auth path stays rejected in the sense the design record meant: one place verifies callers (`ClaimMiddleware`), now with three credential types, and `tool_guard` is unchanged except the allowlist check.

### 3.10 Caller middleware and the tool allowlist

`ClaimMiddleware` (name kept, tests keep importing it) takes `bearer: BearerResolver | None` and `public_url: str | None`. Order per request after the body is buffered: header `X-PrivacyFence-Claim` present: the claim path exactly as today (bare 401 on failure); else `Authorization: Bearer <token>` present: `bearer.resolve(token)`; else the dev identity when set; else 401. A bearer 401 carries `WWW-Authenticate: Bearer resource_metadata="<KH_PUBLIC_URL>/.well-known/oauth-protected-resource/mcp"` and, when a token was sent, `, error="invalid_token"`; a 401 with no credentials carries the first form when OAuth is on. `auth/bearer.py` `BearerResolver.resolve(token) -> Caller | None` sends `khk_` tokens to the key table and `kha_` tokens to the OAuth table; anything else is `None`. For OAuth people the audiences come from `MembershipView.current()` at the call. `Caller` gains `tools: frozenset[str] | None = None` (`None` means all); `tool_guard` refuses a tool outside it with the fixed text `this credential may not call this tool`. Rejection codes `bearer_unknown`, `bearer_expired`, `bearer_resource` are added to `metrics.REJECTION_CODES`. A bearer caller is not bound to the body hash, so `kh_call` is parsed from the body only for the consistency check.

### 3.11 The PrivacyFence path and its limits

While `KH_PF_*` is set the claim path, the `service` principal and `list_audiences` work as today; `resolve_audiences` and `list_audiences` answer from `MembershipView.current()` so a person whose groups come only from the directory keeps them in PrivacyFence. With `legacy_privacyfence: true` the table limits apply to the static table at load and to the effective table when a snapshot is adopted (`sync`, and `MembershipCache.refresh` logs and keeps the previous membership if the new one breaks them). They can be dropped for a deployment now by setting `legacy_privacyfence: false`; the code goes in the PrivacyFence follow-up plan. Existing PrivacyFence-minted n8n tokens keep working until each workflow is moved to a hub key (3.15).

### 3.12 Forgejo

Teams and repos stay driven by `kh setup apply` from the effective membership: `ForgejoStep` takes a `Membership` (default `Membership.from_config`) instead of reading `gc.members` and `gc.approvers`. A directory-sourced login whose Forgejo account does not exist yet prints `i account <login>: no Forgejo account yet; not added to its teams` and is left out of the teams until it exists (a static login keeps `! account <login>: does not exist`). A missing or stale snapshot with a provider configured prints `! directory: snapshot missing` or `! directory: snapshot is <n>s old (limit <m>s)`; the daily drift check fails on it, which is the alert. Forgejo sign-in still goes through PrivacyFence's OIDC until a separate compose change.

### 3.13 Revocation

| Event | Takes effect |
|---|---|
| Hub key revoked or expired | the next request (lookup per request) |
| OAuth token revoked, or its refresh family revoked | the next request |
| Person removed from a Google or Keycloak group, or suspended or disabled | after the next sync (`refresh_seconds`, 300 s) plus `KH_DIRECTORY_VIEW_SECONDS` (30 s): at most about 6 minutes; `kh directory sync --once` forces it |
| Person loses every group | next refresh fails with `invalid_grant` (the access token already reads nothing) |
| Directory unreachable | last good snapshot used until `max_stale_seconds` (6 h), then directory-derived groups disappear |

### 3.14 Deployment and secrets (hand-off, `docs/plans/58/compose-handoff.md`)

The hub needs a public HTTPS name, egress to the upstream IdP, a second service for the sync, and its secrets as files: the OIDC client secret, the Google service account key or the Keycloak client secret. Nothing secret enters `kh.yaml` or the repository. Only the paths listed in 3.9 are public; `/webhooks/forgejo`, `/metrics`, `/healthz` stay internal; `KH_ALLOWED_HOSTS` gets the public host.

### 3.15 Migration and rollback

1. Deploy the new image with nothing configured: no new behaviour; `kh migrate` adds `0006` (additive; the previous image ignores the tables). Rollback: previous image.
2. Directory: set `directory` and the per-group references, run `kh directory sync --once` and `kh directory plan`; compare `kh directory people --json` with the people the compose role rendered; then run the sync service. Rollback: `directory.provider: none` (static people again; the snapshot is ignored).
3. n8n: for each workflow create a key with the group and tools its PrivacyFence token had, change the workflow's credential to a bearer header, check one run, then revoke the PrivacyFence token in PrivacyFence. Rollback per workflow: put the old token back until it is revoked.
4. Claude: add the hub URL as a custom connector, sign in, test, then drop the PrivacyFence connector. Rollback: re-add the PrivacyFence connector (the claim path is unchanged).
5. Later (separate plan): PrivacyFence retires `audiences_tool`; the hub then sets `legacy_privacyfence: false` and unsets `KH_PF_*`.

### 3.16 Tests

Unit: rules (every skip reason, collisions, approver not a member, archived groups, static merge, limit check), config shape, key format and hashing, the upstream ID-token matrix, redirect allowlist, middleware paths, Google and Keycloak providers on `httpx.MockTransport` fixtures. Integration (Postgres): migration `0006` and RLS (no `kh_app` table access), definer functions, snapshot store and cache, sync with a fake provider, token store, refresh rotation and reuse, `kh setup` with a snapshot. Whole flow (`tests/integration/test_oauth_login_to_group_flow.py`): fake IdP (`tests/support/fake_idp.py`) plus a fake directory provider plus the ASGI app: register, authorize, sign in, consent, token, `/mcp` search that sees only the directory-derived group, a removal in the fake directory followed by a sync and cache refresh that hides it, refresh refused after the person loses every group, a key call, and a PrivacyFence claim call. Keycloak live provider test (`tests/integration/test_directory_keycloak_live.py`) skipped unless `KH_TEST_KEYCLOAK_URL` is set; the CI job that sets it is a protected change (the implement orchestrator reports it, Risks).

### 3.17 Security review notes

Token validation: 3.9 item 7 is the checklist (issuer, audience, nonce, `email_verified`, hosted domain, algorithms) and `tests/unit/test_oauth_upstream.py` has one test per refusal. Service-account scope: 3.5 (two read-only scopes, dedicated account, custom role). Residuals added to `docs/design.md`: a caller holding two groups can write into the wider one and the PrivacyFence popup no longer guards OAuth people; a person who is suspended in Google keeps a token until the next sync; registered clients are anonymous public clients; the consent page is the only phishing guard.

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
7. In `kh-directory`, **Service accounts roles** tab, **Assign role**, filter by clients, select `realm-management` roles `view-users` and `query-groups`. You see both listed.
8. **Credentials** tab: copy the secret into the server file `kh_directory_keycloak_secret`.
9. **Groups**: make sure `kh-all` and `kh-all-approvers` exist (the compose role creates `kh-<group>` and `kh-<group>-approvers`); add one test user to each. No `groups` claim mapper is needed: the hub reads groups from the admin API, not from tokens.
Report back: pass/fail for "a test user is in `kh-all` and the two clients exist".

**ma2-google-oauth-client: the Google OAuth client (about 15 minutes).** Needs a Google Cloud project owned by the Workspace organisation.

1. Open `https://console.cloud.google.com/apis/credentials`, pick the project.
2. Configure the consent screen first if the page asks (user type **Internal**, app name `Knowledge hub`, support address, your domain).
3. **Create credentials**, **OAuth client ID**, application type **Web application**, name `knowledge-hub`.
4. Authorized redirect URI: `https://<public hub host>/oauth/callback`. Create. You see a client ID and secret.
5. Put the client ID into `KH_OIDC_CLIENT_ID` (a variable, not secret) and the secret into the server file `kh_oidc_client_secret`.
6. Set `KH_OIDC_ISSUER=https://accounts.google.com`, `KH_OIDC_HOSTED_DOMAIN=<your domain>` and `KH_OIDC_ALLOWED_DOMAINS=<your domain>`.
Report back: pass/fail for "the client exists and the redirect URI matches the hub".

**ma3-google-service-account: the service account and domain-wide delegation (about 30 minutes).** Needs a Workspace super administrator.

1. Enable the Admin SDK API at `https://console.cloud.google.com/apis/library/admin.googleapis.com` in the project.
2. `https://console.cloud.google.com/iam-admin/serviceaccounts`, **Create service account** `kh-directory`, no project roles. Open it, **Keys**, **Add key**, JSON: a file downloads. Move it to the server file `kh_directory_google_key` (mode 0400) and delete the download.
3. Copy the service account's **Unique ID** (the numeric client ID).
4. `https://admin.google.com/ac/users`: create a dedicated user `kh-directory-reader@<your domain>` (no mailbox use, strong password stored by the administrators, no other roles).
5. `https://admin.google.com/ac/roles`: **Create new role** `kh-directory-read` with exactly the privileges **Admin console privileges, Users, Read** and **Groups, Read**. Assign it to `kh-directory-reader@<your domain>` (**Assign admin** on that role). You see the user listed under the role.
6. `https://admin.google.com/ac/owl/domainwidedelegation`: **Add new**, Client ID the Unique ID, OAuth scopes exactly `https://www.googleapis.com/auth/admin.directory.group.member.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly`. Authorise. You see the entry.
7. Set `KH_DIRECTORY_GOOGLE_SUBJECT=kh-directory-reader@<your domain>`.
Report back: pass/fail for "`kh directory sync --once` prints `directory: google <n> users`" (after step ma5 starts the service).

**ma4-google-groups: the Google groups (about 15 minutes).** Needs Groups admin rights.

1. `https://admin.google.com/ac/groups`, **Create group**: `kh-all@<your domain>` (members) and `kh-all-approvers@<your domain>` (approvers). Repeat for every kh group (for example `sensitive`).
2. Add people (or nested groups) as members. Every approver must also be in the members group, directly or through nesting; an approver who is not a member is skipped with a warning.
3. In **Access settings** keep who-can-join restricted; a group that contains the whole domain is refused by the hub on purpose.
4. Put the addresses in `kh.yaml` under `groups.<g>.directory.google` and set `directory.provider: google`.
Report back: pass/fail for "`kh directory plan` lists the expected people and no unexpected skip".

**ma5-deploy-and-probe: deploy the compose hand-off and probe the live system (about 1 hour).** Needs the compose changes of the hand-off applied and the hub released.

1. Converge the compose stack per the hand-off; run `kh migrate`.
2. Check `https://<public hub host>/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server` return JSON, and that `/metrics` and `/webhooks/forgejo` return 404 or are unreachable from the internet.
3. In claude.ai, **Settings**, **Connectors**, **Add custom connector** with `https://<public hub host>/mcp`. You see the Google (or Keycloak) sign-in, then the hub's consent page naming the client and your email; allow it.
4. Ask Claude to run a hub search; it returns only your groups' content. Remove yourself from the Google group, run `kh directory sync --once`, wait 30 seconds, search again: the group's content is gone.
5. In Claude Code run `claude mcp add --transport http kh https://<public hub host>/mcp` and finish the browser sign-in; run a search.
6. Check `kh setup plan` exits 0 and `systemctl status` of the sync service is active.
Report back: pass/fail for each of steps 2 to 6.

**ma6-n8n-cutover: move n8n workflows to hub keys (about 15 minutes per workflow).** Needs `kh key` available in the one-off container and n8n 2.48.5 or newer.

1. For each workflow, run `kh key create --group <g> --tools <the tools its token had> --name <workflow>`; copy the key line into n8n's credential store as a header credential `Authorization: Bearer <key>` (never into chat).
2. Change the workflow's MCP client node to the new credential and run it once; you see a successful `search` or `ingest_record`.
3. In PrivacyFence **Settings, Scoped tokens**, revoke the old token.
4. Run `kh key list`; each workflow has a key and `last_used` is recent.
Report back: pass/fail per workflow, and the line "n8n's MCP client node accepts the hub's streamable HTTP endpoint" (unconfirmed in research).

## 5. Risks and open questions

1. **Claude's registration and refresh behaviour is documented, not tested here** (research r-20261119-2, medium confidence, draft spec page). If claude.ai refuses the registration response or loops on sign-in in ma5, a worker or the human sees a 400 or a redirect loop on `/authorize`; the fallback is a pre-registered client (`kh client`, not built) or CIMD. Stop condition for p10 and p11: if the SDK's `create_auth_routes` or provider protocol differs from the signatures in 3.9 (checked against `mcp==2.3.0`), stop with `status=blocked`.
2. **Google without delegation is unconfirmed.** If the organisation forbids domain-wide delegation, the Google provider needs the Cloud Identity API (paid edition, Group Administrator role on the service account) and a separate user-state call; that is a follow-up spike, not in this plan.
3. **n8n's MCP client with streamable HTTP is unconfirmed** (n8n docs list Bearer and header auth but mention SSE). ma6 settles it; there is no SSE endpoint to fall back to.
4. **`provider.authorize` contract.** The consent page runs after the upstream callback, so the code is issued by the hub's own callback route, not by the SDK's authorize handler; p10 builds the redirect with `construct_redirect_uri`. If the SDK's token handler needs state the hub does not store, p10 stops blocked.
5. **Sync boundary needs a compose service.** Until the hand-off lands the directory data is never fetched; `kh setup plan` then prints `! directory: snapshot missing` when a provider is configured. That is intended.
6. **Shrink guard.** A real mass removal is refused until `--accept-shrink`; the sync logs the refusal every cycle.
7. **Different `sub` per path** (3.2): cursors and the usage hash of one person change when they move from PrivacyFence to OAuth.
8. **Personal data in new tables:** `oauth_tokens`, `oauth_codes`, `oauth_consents` and the snapshot hold email addresses; `kh daily` purges expired token rows and unused clients; the snapshot is replaced each sync.
9. **Protected path.** The Keycloak CI job belongs in `.github/workflows/ci.yml`, a protected path: no phase edits it; the implement orchestrator reports it as `protected-change:` and the owner applies it. Until then the live Keycloak test is skipped.
10. **Registration is open by design** (rate limit, cap, allowlist, consent). Dynamic registration is marked deprecated in the draft spec; if Claude moves to CIMD only, a later change is needed.
11. **Login collisions** (`anna@a.org`, `anna@b.org`) skip both people; the compose role stopped the converge instead. The skip is reported by `kh directory plan`.
12. **Existing tests build `Caller(...)`, `ClaimMiddleware(...)` and `KhConfig(...)` directly** (`tests/support/principals.py`); every new parameter has a default so they keep working. A worker who has to change an existing assertion stops blocked.

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
  - .venv/bin/python -m pytest -q -k oauth
  - .venv/bin/python -m pytest -q -k keys
  - .venv/bin/python -m pytest -q -k bearer
  - .venv/bin/python -m pytest -q -k membership
final_checks:
  - "`tld plan deviations check --file docs/plans/58/deviations.md --plan docs/plans/58/plan.md --all` exits 0"
  - "`grep -n \"kh directory\" CHANGELOG.md` matches under `## [Unreleased]` and the changelog has no new version heading"
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
      Set up `.venv` as README.md Quick start says. Write the tests of step 4 first and see them fail.
      1. `config.py`: add `GroupDirectoryConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`, `members: list[str] = []`, `approvers: list[str] = []`; each entry a non-empty string of at most 254 characters, no duplicates within a list (error `directory reference <x> is listed twice`). Add the field `directory: dict[str, GroupDirectoryConfig] = {}` to `GroupConfig`; its keys must match `^[a-z][a-z0-9_]{0,30}$` (error `directory provider name <k> is not valid`). Change `GroupConfig.approvers` to `list[str] = []`.
      2. `config.py`: add `DirectoryConfig(BaseModel)` (extra forbid): `provider: str = "none"` matching `^(none|[a-z][a-z0-9_]{0,30})$`; `domains: list[str] = []` (lower-cased, unique, each matching `^[a-z0-9][a-z0-9.-]{0,252}$`); `refresh_seconds: int = 300` (60 to 3600); `max_stale_seconds: int = 21600` (at most 604800, at least twice `refresh_seconds`, error `directory.max_stale_seconds must be at least twice refresh_seconds`); `shrink_guard_percent: int = 50` (1 to 99). A model validator refuses `provider != "none"` with empty `domains` (error `directory.domains is required when directory.provider is set`). Add to `KhConfig`: `directory: DirectoryConfig = DirectoryConfig()` and `legacy_privacyfence: bool = True`.
      3. `config.py` `_validate`: a group with no static `approvers` is allowed only when some entry of `group.directory` has a non-empty `approvers` (otherwise keep `group <g> needs at least one approver`); the check that a static approver is a static member is skipped when some entry of `group.directory` has a non-empty `members`. Move the three audience-limit checks (200, 20000, 1,000,000 characters, same messages) into a module-level function `check_audience_limits(table: Mapping[str, Sequence[str]]) -> None` raising `ValueError`, and call it from `_validate` only when `self.legacy_privacyfence` is true. `KhConfig.audiences_for_email` and `audience_members` stay unchanged.
      4. `settings.py`: add the fields of plan 3.3 with the names `public_url`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret_file`, `oidc_allowed_domains` (str, default ""), `oidc_hosted_domain`, `oauth_dcr` (bool, True), `oauth_redirect_allowlist` (str, the default of 3.3), `oauth_access_ttl_seconds` (3600, 300 to 86400), `oauth_refresh_ttl_days` (30, 1 to 365), `oauth_max_clients` (200, 1 to 10000), `oauth_register_per_min` (30, 1 to 1000), `directory_view_seconds` (30, 5 to 600), `directory_google_credentials_file`, `directory_google_subject`, `directory_keycloak_url`, `directory_keycloak_realm`, `directory_keycloak_client_id`, `directory_keycloak_client_secret_file`. In `_check_env_rules`: when any of `public_url`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret_file` is set, all must be set, else `ValueError("KH_OIDC_* and KH_PUBLIC_URL must be set together")`; when OAuth is on and `oidc_allowed_domains` is empty, `ValueError("KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on")`; `public_url` must be `https://<host>` with no path, query or fragment, or `http://` when `env` is `dev` or `test` (error `KH_PUBLIC_URL must be an https URL without a path`). Add properties `oauth_enabled: bool`, `resource_url: str | None` (`<public_url>/mcp`), `allowed_domains_tuple: tuple[str, ...]` (lower-case, trimmed, empty entries dropped) and `redirect_allowlist_tuple: tuple[str, ...]`.
      5. Tests: extend `tests/unit/test_config.py` with a test per rule above (including: empty approvers with a directory approvers list loads; without it the old error; `legacy_privacyfence: false` loads a table of 201 groups that the default refuses with the old message; `directory.provider: google` without domains refused; unknown key under `directory` refused; `deploy/kh.example.yaml` still loads). Extend `tests/unit/test_settings.py` with a test per settings rule (partial OIDC set, https rule, ranges, defaults, `oauth_enabled`).
      Stop with `status=blocked` if an existing test must change an assertion to pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_settings.py -q passes"
      - "grep -n \"def check_audience_limits\" src/knowledge_hub/config.py matches"
      - "grep -n \"legacy_privacyfence\" src/knowledge_hub/config.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/config.py src/knowledge_hub/settings.py tests/unit/test_config.py tests/unit/test_settings.py passes"

  - id: p2-migration-identity
    title: Migration 0006 (keys, OAuth state, directory snapshot) and the daily purge
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0006_identity.sql
      - src/knowledge_hub/jobs/daily.py
      - tests/integration/test_migration_0006.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_setup_postgres.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_daily.py
    brief: |
      Read first: plan section 3.8, src/knowledge_hub/migrations/0001_baseline.sql (lines 120-240, the `kh.claim_seen` pattern), src/knowledge_hub/migrations/0005_move_record.sql, tests/integration/test_migration_0004.py, tests/integration/test_rls_tables.py, tests/integration/test_schema_tables.py, tests/integration/test_daily.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv` as README.md Quick start says.
      1. Create `0006_identity.sql` with the seven tables of plan 3.8 exactly as written, each with `ENABLE` and `FORCE ROW LEVEL SECURITY` and a policy `kh_migrator_all ... FOR ALL TO kh_migrator USING (true) WITH CHECK (true)`.
      2. Create the functions of the table in plan 3.8, each `SECURITY DEFINER`, `SET search_path = kh, pg_temp`, `REVOKE EXECUTE ... FROM PUBLIC`, then `GRANT EXECUTE` to `kh_app` for every function except `directory_snapshot_put` and `oauth_purge`; `directory_snapshot_put` and `oauth_purge` to `kh_indexer` only; `directory_snapshot_get` to `kh_app`, `kh_indexer` and `kh_migrator`. Add one function not in the table: `oauth_pending_peek(p_state bytea) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)`, the same as `oauth_pending_take` without the delete, granted to `kh_app`. `oauth_refresh_use` follows this shape: `SELECT used_at, family_id INTO ... FROM kh.oauth_tokens WHERE token_sha256 = p_token AND kind = 'refresh' AND revoked_at IS NULL AND expires_at > now() FOR UPDATE; IF NOT FOUND THEN RETURN 'gone'; IF used_at IS NULL THEN UPDATE ... SET used_at = now() WHERE token_sha256 = p_token; RETURN 'ok'; IF used_at > now() - make_interval(secs => p_grace_seconds) THEN RETURN 'reuse_grace'; UPDATE kh.oauth_tokens SET revoked_at = now() WHERE family_id = v_family AND revoked_at IS NULL; RETURN 'reuse';`. `oauth_token_revoke` revokes the whole family when the token is a refresh token and only the token when it is an access token. `oauth_purge` deletes pending and code rows past `expires_at`, token rows more than one day past `expires_at`, and clients with `last_used_at < now() - interval '30 days'`, and returns the sum of deleted rows.
      3. `jobs/daily.py`: add `async def purge_identity(indexer_pool) -> int` that runs `SELECT kh.oauth_purge()` in a transaction and returns the number. Do not change `run_daily`.
      4. Create `tests/integration/test_migration_0006.py`: the seven tables exist with row security forced; `kh_app` gets `permission denied` on `SELECT` from each table; each function behaves as the table says (expired or revoked key not returned; `api_key_touch` writes at most once a minute; pending and code `take` return the row once and then nothing; `oauth_pending_peek` does not delete; `oauth_refresh_use` returns `ok`, then `reuse_grace` with a grace of 60, then `reuse` with a grace of 0 and the family's other tokens are revoked, then `gone`; `directory_snapshot_put` is refused for `kh_app` and works for `kh_indexer`; `oauth_purge` counts). Update the existing integration tests that list migrations, tables or policies (`test_migrate_runner.py`, `test_setup_postgres.py`, `test_rls_policies.py`, `test_rls_tables.py`, `test_schema_tables.py`) so they expect the new migration and tables; extend `test_daily.py` with a `purge_identity` test.
      Stop with `status=blocked` if the SQL of plan 3.8 does not apply as written; fix only the failing statement and write the change in the deviations log.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_migration_0006.py tests/integration/test_migrate_runner.py tests/integration/test_setup_postgres.py tests/integration/test_rls_policies.py tests/integration/test_rls_tables.py tests/integration/test_schema_tables.py tests/integration/test_daily.py -q passes"
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
      1. `directory/__init__.py` empty (a docstring only). `directory/model.py`: the dataclasses and the `MembershipView` protocol of plan 3.4, with `Membership.from_config(config)` (static people, members and approvers of every group, archived groups included, logins taken from the config as written and emails lower-case), `audiences_for_email(email, is_active)` (lower-case the email, find the logins whose email matches, return the groups of `members` that contain one of them and for which `is_active(group)` is true) and `audience_table(is_active)` (group to sorted, de-duplicated emails of its members, for active groups only).
      2. `directory/rules.py`: `login_from_email`, `classify`, `effective` exactly as plan 3.4 says, with the reason strings `disabled`, `email not verified`, `no valid email`, `domain not allowed`, `not a valid login`, `bot or reserved login`, `login shared by several emails`, `approver is not a member`. `effective` ignores directory data for archived groups and for groups absent from `fetched`. It never raises on user data.
      3. `directory/provider.py`: `GroupRefs`, `ProviderError`, `DirectoryProvider`, `build_provider(name, settings, *, http=None)` (an `importlib` lookup in the table `{"google": "knowledge_hub.directory.google", "keycloak": "knowledge_hub.directory.keycloak"}`; an unknown name raises `ValueError("directory: unknown provider <name>")`) and `refs_from_config(config, provider) -> dict[str, GroupRefs]` (active groups whose `directory[provider]` has at least one reference).
      4. Tests, one behaviour each: every skip reason; reason order (a disabled user with a foreign domain gets `disabled`); `login_from_email` accepts `Anna.Kiss@Example.com` as `anna.kiss`, refuses `a+b@x.org` and an address with two `@`; a collision between two directory emails with one login skips both; a static `people` login wins over a directory email and the directory person is skipped; an approver who is not a member is skipped and not an approver; a static approver with no membership after the merge is skipped with the same reason; static entries survive an empty fetch; archived groups get no directory data; `fetched=None` equals `Membership.from_config`; for the config of `tests/support/principals.py` `audience_table` equals `KH_CONFIG.audience_members()` and `audiences_for_email` equals `KH_CONFIG.audiences_for_email` for every person; `build_provider` refuses an unknown name; `refs_from_config` skips archived groups and groups without references.
      Do not import `google` or `keycloak` modules from this phase's files. Stop with `status=blocked` if `check_audience_limits` is missing from `config.py`.
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
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first.
      1. `claims.py`: add `tools: frozenset[str] | None = None` as the last field of `Caller` (existing constructors keep working) and nothing else in this file.
      2. `mcp/errors.py`: add `TOOL_NOT_ALLOWED = "this credential may not call this tool"` and, in `tool_guard`'s wrapper right after the `isinstance(call, Call)` and tool-name checks, `if caller.tools is not None and name not in caller.tools:` record `METRICS.tool_call(name, "error", 0.0)` and `raise ToolError(TOOL_NOT_ALLOWED)`.
      3. `auth/keys.py`: `KEY_RE`, `new_key() -> tuple[str, str, bytes]` (id, full key `khk_<id>.<secret>`, `sha256` of the secret) per plan 3.7, `parse_key(text) -> tuple[str, str] | None`, `KEY_TOOLS` (the registered tool names minus `list_audiences` and `move_item`, as a sorted tuple written out literally), `async def verify_key(pool, config, text) -> Caller | None` (calls `kh.api_key_get`, compares with `hmac.compare_digest`, requires `config.is_active(audience)`, calls `kh.api_key_touch`, returns `Caller(sub=f"key:{id}", principal_type="n8n_workflow", email=None, audiences=frozenset({audience}), token_id=id, tools=frozenset(tools))`).
      4. `auth/keys_cli.py`: `main(argv) -> int` with the subcommands, outputs and errors of plan 3.7, using `load_settings()`, `load_config(settings.config_file)` and a psycopg connection to `settings.dsn("migrator")`. `cli.py`: in `main`, before the argparse block, `if argv and argv[0] == "key":` import and run `knowledge_hub.auth.keys_cli.main(argv[1:])`. The module must not be imported by `app.py`.
      5. Tests: `tests/unit/test_hub_keys.py` (format regex, parse refusals, two keys differ, hash is sha256 of the secret, `KEY_TOOLS` equals the names `build_mcp(deps)` registers minus the two refused ones, `tool_guard` refuses a tool outside `Caller.tools` with the fixed text and allows it inside, `tools=None` allows all). `tests/integration/test_hub_keys_db.py` (create then verify works; wrong secret, revoked, expired, inactive group, archived group each give `None`; `list --json` shows no secret or hash; `revoke` of an unknown id exits 1; `last_used_at` moves once within a minute).
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
      1. `oauth/__init__.py`: a docstring only. `oauth/upstream.py`: `UpstreamConfig` (frozen dataclass: `issuer`, `client_id`, `client_secret`, `allowed_domains: tuple[str, ...]`, `hosted_domain: str | None`, `redirect_uri`, `allow_http: bool`), `UpstreamError(Exception)` with a short `code`, `VerifiedIdentity` (`email`, `sub`, `hd: str | None`), and `UpstreamClient(config, http: httpx.AsyncClient)` with `async discover()` (cached for one hour), `new_pkce() -> tuple[str, str]` (a 43-character verifier and its S256 challenge), `async authorization_url(state, nonce, challenge) -> str`, `async exchange(code, verifier) -> dict` (`client_secret_post`), `async verify_id_token(id_token, nonce) -> VerifiedIdentity`. Every check of plan 3.9 item 7 is implemented with PyJWT: fetch the discovery JWKS with `httpx` (cache five minutes, refetch once on an unknown `kid`), `jwt.decode(..., algorithms=["RS256", "ES256"], audience=client_id, issuer=<discovered issuer>, leeway=60, options={"require": ["exp", "iat", "sub"]})`, then the explicit checks of `nonce`, `azp` (when `aud` is a list of more than one), `email_verified is True`, the domain list and the hosted domain. Return the email lower-cased. Never call a userinfo endpoint.
      2. `tests/support/fake_idp.py`: a Starlette app `make_fake_idp(issuer, client_id, client_secret)` with `/.well-known/openid-configuration`, `/jwks`, `/authorize` (redirects to the `redirect_uri` with a fixed `code` and the request's `state`), `/token` (checks `client_id`, `client_secret`, `code`, `code_verifier` against the stored challenge and returns an `id_token` signed with an in-process RSA key), and a mutable `claims` dict the test edits (`email`, `email_verified`, `hd`, `aud`, `iss`, `nonce` taken from the authorize request unless overridden). It is also used by the flow test of p14, so give it a method `sign(claims, alg="RS256", kid=None) -> str` that tests call directly.
      3. Tests in `tests/unit/test_oauth_upstream.py`, one per refusal and one per success: a valid token; wrong `iss`; wrong `aud`; several audiences with the wrong `azp`; `alg` `none` (hand-built); HS256 signed with the public key bytes; expired; `iat` missing; nonce mismatch; `email_verified` missing, `"true"` as a string, and `false`; email domain not allowed; `hd` different; `hd` missing while a hosted domain is set; email domain different from `hd`; unknown `kid` refetches once and then fails; tampered signature; discovery whose `issuer` differs from the configured one; an `http` endpoint refused unless `allow_http`; the authorization URL carries `response_type=code`, `scope=openid email`, `code_challenge_method=S256`, the state, nonce and `hd`. Use `httpx.ASGITransport(app=fake_idp)`; no network.
      Stop with `status=blocked` if PyJWT cannot verify an `ES256` token from the fake IdP key; use `RS256` only in the fake and say so in the deviations log.
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
      Write the tests first and see them fail. No test may reach the network.
      1. `directory/google.py`: `make(settings, http)` returning `GoogleProvider(credentials_file, subject, http)`. It reads the service account JSON (`client_email`, `private_key`, `token_uri`), refuses a `token_uri` other than `https://oauth2.googleapis.com/token`, signs the assertion with PyJWT `RS256` as plan 3.5 says (scopes exactly `https://www.googleapis.com/auth/admin.directory.group.member.readonly` and `https://www.googleapis.com/auth/admin.directory.user.readonly`), caches the token until five minutes before expiry, and implements `fetch(refs)` and `aclose()` with the members, users, paging, retry and error rules of plan 3.5. A missing credentials file or subject raises `ProviderError("google: KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE and KH_DIRECTORY_GOOGLE_SUBJECT are required")`.
      2. `tests/support/google_fake.py`: `make_google_transport(groups, users, *, fail=None) -> httpx.MockTransport` serving the token endpoint, `groups/<key>/members` (with `pageToken` paging and `includeDerivedMembership` required in the query) and `users` (paged), recording each request, with switches for 404, 429 once, 500 always and a `CUSTOMER` row. Test RSA key and key file are generated in the test with `cryptography` and `tmp_path`.
      3. Tests: the assertion decodes (with the test public key) to `iss`, `sub`, `scope`, `aud` as required; a second fetch reuses the token; paging over two pages; a `GROUP` row is ignored; a `CUSTOMER` row fails the whole fetch with `group <ref> contains the whole domain`; 404 raises `ProviderError`; one 429 is retried and succeeds; persistent 500 raises after three tries; a suspended and an archived user are not enabled; an alias address maps to its user; an own-domain member missing from `users` is not enabled; an external member keeps its own address; a wrong `token_uri` is refused; no request carries a scope outside the two.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_google.py -q passes"
      - "grep -c \"readonly\" src/knowledge_hub/directory/google.py prints 2 or more"
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
    brief: |
      Read first: plan section 3.5 (Keycloak), src/knowledge_hub/directory/provider.py, src/knowledge_hub/directory/model.py.
      Write the tests first and see them fail.
      1. `directory/keycloak.py`: `make(settings, http)` returning `KeycloakProvider(url, realm, client_id, client_secret, http)` and `fetch(refs)` per plan 3.5 (client-credentials token, group lookup by path or by exact top-level name, members with paging of 100, recursion into `children` with depth at most 10 and a visited set, `briefRepresentation=false`). A missing setting raises `ProviderError("keycloak: KH_DIRECTORY_KEYCLOAK_URL, _REALM, _CLIENT_ID and _CLIENT_SECRET_FILE are required")`. A group that is not found, a 401, 403, 5xx after three tries, or a malformed page raises `ProviderError`; an empty group is a valid empty list.
      2. `tests/unit/test_directory_keycloak.py` with `httpx.MockTransport`: top-level lookup keeps only the result whose `path` is `/<name>`; a path reference uses `group-by-path`; two pages of members; a nested group's members are included and a cycle does not loop; `enabled` and `emailVerified` are carried through; a user without `email` is returned with an empty email; 403 raises; a missing group raises; the token request uses `client_credentials`.
      3. `tests/support/keycloak_realm.json`: a Keycloak realm import for the realm `kh-test` with the clients `kh-hub` (confidential, standard flow, redirect `http://localhost:8080/oauth/callback`, PKCE `S256`) and `kh-directory` (service accounts on, with `view-users` and `query-groups`), groups `kh-all` and `kh-all-approvers` (and a child group `kh-all/team`), and users: two enabled and verified in `kh-all`, one in `kh-all-approvers` only, one disabled, one with an unverified email, one with a foreign-domain email, one in the child group. Client secrets in the file are the literal text `test-only-not-a-secret`.
      4. `tests/integration/test_directory_keycloak_live.py`: skipped (`pytest.mark.skipif`) unless `KH_TEST_KEYCLOAK_URL`, `KH_TEST_KEYCLOAK_REALM`, `KH_TEST_KEYCLOAK_CLIENT_ID` and `KH_TEST_KEYCLOAK_CLIENT_SECRET` are set; with them it fetches `kh-all` and `kh-all-approvers` and, through `rules.effective`, asserts the skips and the members the realm file defines. Do not edit `.github/workflows/ci.yml`: report `protected-change: .github/workflows/ci.yml - add a keycloak job that starts quay.io/keycloak/keycloak:26.4.0 with tests/support/keycloak_realm.json imported and sets the four KH_TEST_KEYCLOAK_ variables` with the unified diff of the new job, as the protected-changes policy says.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_keycloak.py tests/integration/test_directory_keycloak_live.py -q passes"
      - ".venv/bin/python -c \"import json; json.load(open('tests/support/keycloak_realm.json'))\" exits 0"
      - "grep -rn \"keycloak\" src/knowledge_hub/directory/model.py src/knowledge_hub/directory/rules.py src/knowledge_hub/config.py prints nothing"
      - ".venv/bin/python -m ruff check src/knowledge_hub/directory/keycloak.py tests/unit/test_directory_keycloak.py tests/integration/test_directory_keycloak_live.py passes"

  - id: p8-directory-sync
    title: Snapshot store, membership cache, sync process and kh directory commands
    depends_on: [p2-migration-identity, p3-directory-core, p4-hub-keys]
    complexity: M
    touches:
      - src/knowledge_hub/directory/store.py
      - src/knowledge_hub/directory/sync.py
      - src/knowledge_hub/directory/cli.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/metrics.py
      - tests/unit/test_directory_sync.py
      - tests/integration/test_directory_snapshot.py
    brief: |
      Read first: plan sections 3.6 and 3.11, src/knowledge_hub/directory/model.py, rules.py, provider.py, src/knowledge_hub/jobs/daily.py (`purge_identity`), src/knowledge_hub/cli.py, src/knowledge_hub/metrics.py, tests/integration/test_daily.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first.
      1. `directory/store.py`: `snapshot_to_fetched(data) -> dict[str, GroupFetch]`, `fetched_to_snapshot(provider, fetched) -> dict`, `async load_snapshot(conn) -> tuple[dict, datetime] | None` (calls `kh.directory_snapshot_get()`), `async put_snapshot(conn, data)`, `class MembershipCache(pool, config, settings, now=time.time)` with `async refresh()`, `current() -> Membership` and `age_seconds() -> float | None` as plan 3.6 says (a snapshot older than `max_stale_seconds`, or no row while `config.directory.provider != "none"`, gives `Membership.from_config(config)`; with the provider `none` the cache is always the static membership), and `async load_membership(config, settings) -> tuple[Membership, float | None]` (plan 3.6: the static membership and `None` without a database call when the provider is `none`; otherwise a one-off connection to `settings.dsn("migrator")`, the same cache rules as `MembershipCache`, and the snapshot age in seconds or `None` when there is no snapshot). `refresh()` runs `effective`, and when `config.legacy_privacyfence` also `check_audience_limits(membership.audience_table(config.is_active))`; on `ValueError` it logs the message and keeps the previous membership.
      2. `directory/sync.py`: `async sync_once(config, settings, provider, pool, *, accept_shrink=False) -> SyncResult` (fields `users`, `groups`, `skips`) and `async run_every(...)`. Follow plan 3.6 exactly: refs from `refs_from_config`, one fetch, the snapshot built, the audience limits checked when `legacy_privacyfence`, the shrink guard (`directory: refusing snapshot: <a> users dropped to <b>; rerun with --accept-shrink`), store, then log `directory: <provider> <n> users in <m> groups, <k> skipped` and one line per skip. Any `ProviderError` leaves the previous snapshot and is re-raised by `sync_once`; `run_every` logs it and waits.
      3. `directory/cli.py`: `main(argv) -> int` with `sync [--once | --every N] [--accept-shrink]`, `plan`, `people [--json]`, `show` as plan 3.6 says; `sync` connects as `kh_indexer` (`settings.dsn("indexer")`), the others read through `load_membership`. Exit 2 on a settings or config error (print it), exit 1 on a provider error with `directory: <provider>: <reason>`. `src/knowledge_hub/cli.py`: dispatch `argv[0] == "directory"` to it; make `_daily` also call `purge_identity(deps.indexer_pool)` and append `; identity rows purged: <n>` to its printed line.
      4. `metrics.py`: add a gauge `kh_directory_snapshot_age_seconds` (a setter `directory_snapshot_age(seconds)` on `Metrics`, rendered in `render` next to the existing blocks, absent until set) and call it from `MembershipCache.refresh`.
      5. Tests: `tests/unit/test_directory_sync.py` (with a fake provider object: a good run stores and logs; a `ProviderError` keeps the old snapshot; the shrink guard refuses at the configured percent and `accept_shrink` allows it; the limit check refuses an oversized table only when `legacy_privacyfence`; `plan` exits 1 with no snapshot; `people --json` output shape). `tests/integration/test_directory_snapshot.py` (put then load round trip as `kh_indexer`; `kh_app` can `get` but not `put`; `MembershipCache` returns the directory groups when fresh, the static membership when older than `max_stale_seconds` (inject `now`), and keeps the old membership when the limit check fails).
      The app must not import `directory.sync`, `directory.cli`, `directory.google` or `directory.keycloak`; only `store.py`, `model.py` and `rules.py` are for the serving process.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_sync.py tests/integration/test_directory_snapshot.py tests/integration/test_daily.py -q passes"
      - "grep -n \"kh_directory_snapshot_age_seconds\" src/knowledge_hub/metrics.py matches"
      - ".venv/bin/python -m ruff check . passes"

  - id: p9-oauth-store
    title: OAuth token helpers and the Postgres store
    depends_on: [p2-migration-identity]
    complexity: S
    touches:
      - src/knowledge_hub/oauth/tokens.py
      - src/knowledge_hub/oauth/store.py
      - tests/integration/test_oauth_store.py
    brief: |
      Read first: plan section 3.8, src/knowledge_hub/auth/replay.py, tests/integration/test_replay_cache.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the test first.
      1. `oauth/tokens.py`: `ACCESS_PREFIX = "kha_"`, `REFRESH_PREFIX = "khr_"`, `new_token(prefix) -> str` (prefix plus 43 characters of unpadded urlsafe base64 of 32 random bytes), `token_hash(token) -> bytes` (`sha256` of the ASCII string), `new_secret() -> str` (the same 43 characters, no prefix, used for states, codes and tickets).
      2. `oauth/store.py`: `OAuthStore(pool)` with one async method per function of plan 3.8 that `kh_app` may call: `client_get`, `client_put`, `client_count`, `pending_put`, `pending_take`, `pending_peek`, `code_put`, `code_take`, `token_put`, `token_get`, `refresh_use`, `token_revoke`, `family_revoke`, `consent_has`, `consent_put`. Row results are frozen dataclasses `PendingRow(client_id, params, nonce, verifier)`, `CodeRow(client_id, email, params)`, `TokenRow(family_id, client_id, email, scopes, resource, expires_at, used_at)`; a missing row is `None`. Every call is one `SELECT kh.<function>(...)` on `pool.connection()`; hashes are passed as `bytes`.
      3. `tests/integration/test_oauth_store.py`: a test per method against the migrated test database (round trips, single use of `pending_take` and `code_take`, `pending_peek` keeps the row, `refresh_use` returns the four outcomes, `family_revoke` hides the family's access tokens, consent round trip, client count).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_oauth_store.py -q passes"
      - "grep -n \"sha256\" src/knowledge_hub/oauth/tokens.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/integration/test_oauth_store.py passes"

  - id: p10-oauth-provider
    title: HubAuthProvider, the SDK authorization-server provider
    depends_on: [p9-oauth-store, p5-oidc-relying-party, p3-directory-core]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/provider.py
      - tests/unit/test_oauth_provider.py
      - tests/integration/test_oauth_provider_db.py
    brief: |
      Read first: plan section 3.9, `mcp.server.auth.provider` and `mcp.server.auth.handlers` of the installed `mcp==2.3.0` (`.venv/lib/python3.*/site-packages/mcp/server/auth/`), src/knowledge_hub/oauth/store.py, tokens.py, upstream.py, src/knowledge_hub/directory/model.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`. Write the tests first.
      1. `oauth/provider.py`: `HubAuthProvider(store, upstream, settings, config, membership, *, now=time.time)` implementing `OAuthAuthorizationServerProvider` with the methods and rules of plan 3.9 items 1, 2, 5 and 6: `get_client`, `register_client` (allowlist, cap, public client only, grant types, `valid_scopes`), `authorize` (checks `resource`, stores the pending row with a 10-minute expiry and returns `await upstream.authorization_url(...)`), `load_authorization_code` (uses `code_take`), `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token`. `load_access_token` returns the SDK `AccessToken` with `subject` the email, `resource` and `scopes` from the row, or `None` for an unknown, expired or revoked token. Refresh follows the four outcomes of 3.9 item 5 and revokes the family with `invalid_grant` when `membership.current().audiences_for_email(email, config.is_active)` is empty.
      2. Add the hub's own methods used by the routes of p11: `async handle_callback(code, state) -> CallbackOutcome`, `async consent_context(ticket) -> ConsentContext | None` (client name, redirect host, email; uses `pending_peek`), `async decide_consent(ticket, allow) -> str` (returns the redirect URL; `Allow` stores the consent and issues the code, `Deny` returns the client redirect with `error=access_denied`), as plan 3.9 items 3 and 4 say; build redirect URLs with `mcp.server.auth.provider.construct_redirect_uri`. `CallbackOutcome` and `ConsentContext` are frozen dataclasses in this file. The consent ticket is a pending row whose `params` hold `{"email": ..., "request": <the original authorization request>}`.
      3. Tests: unit tests with a fake store and a fake upstream (registration: `https://claude.ai/api/mcp/auth_callback` accepted, `http://localhost:54321/cb` and `http://127.0.0.1/x` accepted, `https://evil.example/cb` and `http://evil.example` refused with `invalid_redirect_uri`, a confidential client refused, the cap refused; authorize with a wrong `resource` refused; callback with no group gives `access_denied`; callback with a stored consent issues the code; without one gives a ticket; deny gives `access_denied`). Integration tests on Postgres: a code is single use; access and refresh tokens are stored hashed (the raw token appears in no column); rotation `ok`, then a second use inside the grace gives `invalid_grant` without revoking the family, a later reuse revokes the family; refresh for a person who lost every group gives `invalid_grant` and revokes the family; `revoke_token` of a refresh token hides the family's access tokens.
      Stop with `status=blocked` if the SDK token handler needs the authorization code to come from `authorize` itself.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_provider.py tests/integration/test_oauth_provider_db.py -q passes"
      - "grep -n \"construct_redirect_uri\" src/knowledge_hub/oauth/provider.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/unit/test_oauth_provider.py tests/integration/test_oauth_provider_db.py passes"

  - id: p11-oauth-routes
    title: OAuth routes, callback, consent page and registration limiter
    depends_on: [p10-oauth-provider]
    complexity: M
    touches:
      - src/knowledge_hub/oauth/routes.py
      - src/knowledge_hub/oauth/consent.py
      - tests/unit/test_oauth_routes.py
    brief: |
      Read first: plan sections 3.9 and 3.14, `mcp.server.auth.routes` of the installed `mcp==2.3.0` (`create_auth_routes`, `create_protected_resource_routes`), src/knowledge_hub/oauth/provider.py, src/knowledge_hub/ratelimit.py.
      Set up `.venv`. Write the tests first.
      1. `oauth/consent.py`: `render_consent(client_name, redirect_host, email, ticket, csrf) -> str` returning a complete HTML page (title `Knowledge hub: allow access`, the three values HTML-escaped with `html.escape`, one form with the hidden fields `ticket` and `csrf` and two buttons `decision=allow` and `decision=deny`), and `CONSENT_HEADERS` (the three headers of plan 3.9 item 4 plus `Cache-Control: no-store`).
      2. `oauth/routes.py`: `build_oauth_routes(settings, provider) -> list[BaseRoute]` returning the routes of `create_auth_routes(...)` and `create_protected_resource_routes(...)` with the arguments of plan 3.9, plus `GET /oauth/callback`, `GET /oauth/consent`, `POST /oauth/consent`. The callback passes `code` and `state` to `provider.handle_callback` and answers a 302 to `redirect_url`, or to `/oauth/consent?t=<ticket>`; a request without `state`, or with an unknown or used one, answers 400 `invalid request` (no redirect). The consent GET sets the `kh_csrf` cookie (random `new_secret()`, `HttpOnly`, `SameSite=Strict`, path `/oauth/consent`, `Secure` unless `settings.public_url` starts with `http://`) and renders the page; the POST requires the cookie value to equal the form `csrf` (else 400) and calls `provider.decide_consent(ticket, decision == "allow")`, answering a 302. When `settings.oauth_dcr` is false the registration route is not mounted. The `/register` route is wrapped by a global limiter of `settings.oauth_register_per_min` requests per minute (reuse `RateLimiter` from `ratelimit.py` with the fixed key `register`); a refused call answers 429 with `Retry-After`.
      3. Tests with `httpx.ASGITransport` and a stub provider: the authorization-server metadata lists `issuer`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint` (only with DCR on), `code_challenge_methods_supported` containing `S256` and `token_endpoint_auth_methods_supported` containing `none`; the protected-resource metadata at `/.well-known/oauth-protected-resource/mcp` names `<public_url>/mcp` and the authorization server and both scopes; `/register` answers 429 after the limit; the consent page escapes `<script>` in a client name and carries the three headers; a POST with a missing or different CSRF value is 400; the callback with an unknown state is 400 and sets no cookie.
      Stop with `status=blocked` if `create_auth_routes` has a different signature in `mcp==2.3.0` than plan 3.9 uses.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_oauth_routes.py -q passes"
      - "grep -n \"Content-Security-Policy\" src/knowledge_hub/oauth/consent.py matches"
      - ".venv/bin/python -m ruff check src/knowledge_hub/oauth tests/unit/test_oauth_routes.py passes"

  - id: p12-caller-wiring
    title: Bearer path in the caller middleware, app wiring and list_audiences from the effective membership
    depends_on: [p4-hub-keys, p8-directory-sync, p11-oauth-routes]
    complexity: M
    touches:
      - src/knowledge_hub/auth/claims.py
      - src/knowledge_hub/auth/bearer.py
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
      1. `auth/bearer.py`: `class BearerResolver(pool, config, provider, membership, resource_url)` with `async resolve(token) -> Caller | None`: a token starting with `khk_` goes to `keys.verify_key`; one starting with `kha_` goes to `provider.load_access_token`, which must have `resource == resource_url` (else `None`, metric code `bearer_resource`) and returns `Caller(sub=f"u:{email}", principal_type="claude_client", email=email, audiences=membership.current().audiences_for_email(email, config.is_active), token_id=None, tools=<the union of the tool sets of the token's scopes, plan 3.9 item 6>)`; anything else `None`. Define the two scope-to-tool tuples here as `READ_TOOLS` and `WRITE_TOOLS`.
      2. `claims.py`: `ClaimMiddleware.__init__` gains `bearer: BearerResolver | None = None` and `public_url: str | None = None`; `_Refuse` gains `headers: list[tuple[bytes, bytes]]`; `_handle` follows the order of plan 3.10 (claim header first, then `Authorization: Bearer`, then the dev identity, then 401) and the `WWW-Authenticate` rules of 3.10; the buffered body still feeds `kh_call`. `resolve_audiences(config, principal_type, email, claim_audiences, membership=None)`: when `membership` is given, `claude_client` uses `membership.current().audiences_for_email(email, config.is_active)`; the three-argument call keeps its behaviour. Add `bearer_unknown`, `bearer_expired` and `bearer_resource` to `metrics.REJECTION_CODES`.
      3. `mcp/server.py`: `Deps` gains `membership: Any = None`, `oauth: Any = None`, `bearer: Any = None`. `deps.py`: `make_verifier` returns `None` instead of raising when no PrivacyFence setting and no dev identity is set; `build_deps` builds `MembershipCache` always, and, when `settings.oauth_enabled`, the `UpstreamClient`, `OAuthStore`, `HubAuthProvider` and `BearerResolver`; it logs the line `auth paths: privacyfence=<on|off> oauth=<on|off> keys=on dev_identity=<on|off>`; `close_deps` closes the upstream HTTP client. `app.py`: `build_app` appends `build_oauth_routes(...)` to the routes when OAuth is on; the lifespan builds `ClaimMiddleware` with the resolver and `public_url`, and starts and stops a task that calls `deps.membership.refresh()` every `settings.directory_view_seconds` (the first refresh runs before the first request). The HTTP client of the upstream and the provider's `membership` argument use `deps.membership`.
      4. `tools/audiences.py`: answer from `deps.membership.current().audience_table(config.is_active)` when `deps.membership` is set, else from `config.audience_members()`.
      5. Tests: `tests/unit/test_bearer_middleware.py` (valid key and valid OAuth token reach the app with the right `Caller`; unknown, expired, revoked and wrong-resource tokens give 401 with `WWW-Authenticate` carrying `resource_metadata` and `error="invalid_token"`; no credentials with OAuth on gives 401 with the bare header; a claim header present never falls through to the bearer path; the dev identity still works only without credentials; a key may not call a tool outside its list; `kh:read` only cannot call `ingest_record`). Extend `tests/unit/test_claims.py` and `tests/integration/test_list_audiences.py` (a person whose group comes only from the directory snapshot appears in the answer; the three-argument `resolve_audiences` call is unchanged). In `tests/unit/test_import_boundary.py` add an assertion that importing `knowledge_hub.app` loads none of `knowledge_hub.directory.sync`, `.directory.cli`, `.directory.google`, `.directory.keycloak`, `knowledge_hub.auth.keys_cli`.
      Stop with `status=blocked` if an existing test must change an assertion to pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_bearer_middleware.py tests/unit/test_claims.py tests/unit/test_import_boundary.py tests/integration/test_list_audiences.py tests/integration/test_http_e2e.py -q passes"
      - "grep -n \"resource_metadata\" src/knowledge_hub/auth/claims.py matches"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"

  - id: p13-forgejo-membership
    title: kh setup and the admin commands use the effective membership
    depends_on: [p3-directory-core, p8-directory-sync]
    complexity: M
    touches:
      - src/knowledge_hub/setup/forgejo.py
      - src/knowledge_hub/setup/cli.py
      - src/knowledge_hub/admin/apply.py
      - tests/unit/test_setup_membership.py
      - tests/unit/test_setup_forgejo.py
      - tests/unit/test_setup_cli.py
      - tests/unit/test_admin_apply.py
    brief: |
      Read first: plan section 3.12, src/knowledge_hub/setup/forgejo.py (lines 82-300), src/knowledge_hub/setup/cli.py, src/knowledge_hub/admin/apply.py (the `Steps` class), tests/unit/test_setup_forgejo.py, tests/support/forgejo_admin_fake.py.
      Set up `.venv`. Write the tests first.
      1. `setup/forgejo.py`: `ForgejoStep.__init__` gains `membership_loader: Callable[[], Awaitable[tuple[Membership, float | None]]] | None = None`; with `None` the step uses `(Membership.from_config(config), None)`. A private `async _load()` runs the loader once per `plan`, `apply` and `sync_group` call and stores `self._membership` and `self._snapshot_age`. Replace every read of `gc.members` and `gc.approvers` (the `_group`, `sync_group`, `_accounts` and archived-trim code) with the group's logins from `self._membership`: `sorted(self._membership.members.get(name, ()))` and `sorted(self._membership.approvers.get(name, ()))`. A login that is in `membership.people` but not in `config.people` (directory-sourced) whose Forgejo account is missing prints `i account <login>: no Forgejo account yet; not added to its teams` and is left out of the teams; a login from `config.people` keeps `! account <login>: does not exist`. A new method `directory_changes() -> list[Change]`, called at the end of `_sync`, prints `! directory: snapshot missing` when `config.directory.provider != "none"` and `self._snapshot_age is None`, `! directory: snapshot is <n>s old (limit <m>s)` when the age exceeds `config.directory.max_stale_seconds`, and `i directory: <n> people from the directory snapshot` otherwise; it prints nothing when the provider is `none`.
      2. `setup/cli.py`: the `forgejo` builder passes `membership_loader=lambda: load_membership(config, settings)` (imported from `knowledge_hub.directory.store`). `admin/apply.py`: the `Steps` factory passes the same loader when it builds `ForgejoStep`. Nothing loads the snapshot before the postgres step has run.
      3. Tests in `tests/unit/test_setup_membership.py` with `tests/support/forgejo_admin_fake.py`: a group with directory members gets teams containing them; directory approvers are in the approve team; a directory person without an account gets the `i` line and is not in the team; a static person without an account still gets `!`; with a provider set and no snapshot the plan prints `! directory: snapshot missing` and exits 1; with a stale one the age line; with provider `none` nothing new is printed; the existing tests in `test_setup_forgejo.py`, `test_setup_cli.py` and `test_admin_apply.py` pass unchanged (extend them only to pass the new optional argument where a signature requires it).
      Stop with `status=blocked` if an existing assertion must change.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_membership.py tests/unit/test_setup_forgejo.py tests/unit/test_setup_cli.py tests/unit/test_admin_apply.py tests/integration/test_admin_groups.py -q passes"
      - "grep -n \"gc.members\" src/knowledge_hub/setup/forgejo.py prints nothing"
      - ".venv/bin/python -m ruff check . passes"

  - id: p14-flow-tests
    title: Login-to-group flow and HTTP-level security tests
    depends_on: [p12-caller-wiring, p13-forgejo-membership]
    complexity: M
    touches:
      - tests/integration/test_oauth_login_to_group_flow.py
      - tests/integration/test_oauth_http_security.py
      - tests/support/oauth_app.py
    brief: |
      Read first: plan sections 3.9, 3.13 and 3.16, tests/support/fixtures_app.py, tests/support/mcp_client.py, tests/support/fake_idp.py, tests/support/seed.py, src/knowledge_hub/app.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv`.
      1. `tests/support/oauth_app.py`: a fixture `oauth_stack` that builds the real app (`build_app`) over the migrated database with OAuth on (`public_url` `https://hub.test.example`, `KH_ENV=test`), the fake IdP of `tests/support/fake_idp.py` mounted through `httpx.ASGITransport` as the upstream, a `FakeProvider` (a `DirectoryProvider` whose groups the test edits) and a helper `sync()` that runs `sync_once` then `MembershipCache.refresh()`; plus helpers `register_client(redirect_uri)`, `sign_in(email)` (drives `/authorize`, the fake IdP, `/oauth/callback`, `/oauth/consent`) and `mcp_call(token, tool, args)`. Seed one document per group with `tests/support/seed.py`.
      2. `test_oauth_login_to_group_flow.py` (name the tests so `-k oauth` selects them): register with `https://claude.ai/api/mcp/auth_callback`, authorize with PKCE and `resource`, sign in as `alice@example.test` with the fake IdP, consent, exchange the code, call `search` and see only the group the fake directory gave `alice`; remove `alice` from the fake directory group, `sync()`, search again and see nothing; refresh then gives `invalid_grant`; a person in the static `people` of `kh.yaml` only still signs in and reads; a skipped person (disabled in the directory) gets `access_denied`; a hub key created with the key store reads only its group and cannot call a tool outside its list; a PrivacyFence claim built with `tests/support/claims.py` still works and `list_audiences` includes the directory-only person; a revoked key and a revoked OAuth family give 401 on the next call.
      3. `test_oauth_http_security.py`: `/register` with `https://evil.example/cb` is 400 `invalid_redirect_uri`; `/authorize` with an unregistered `redirect_uri` is refused and never redirects; a token whose stored `resource` differs is 401; an access token used after `exp` is 401; the second use of a code is `invalid_grant`; a wrong PKCE verifier is `invalid_grant`; the consent POST without the CSRF cookie is 400 and no code is issued; the upstream callback with an unverified email, a foreign domain or a replayed state gives no code; every 401 from `/mcp` with OAuth on carries `WWW-Authenticate` with `resource_metadata`.
      No test reaches a real network; the only upstream is the fake IdP.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_oauth_login_to_group_flow.py tests/integration/test_oauth_http_security.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"

  - id: p15-docs-design-config
    title: design.md, configuration.md and the example kh.yaml
    depends_on: [p12-caller-wiring, p13-forgejo-membership, p6-google-provider, p7-keycloak-provider]
    complexity: M
    touches:
      - docs/design.md
      - docs/configuration.md
      - deploy/kh.example.yaml
    brief: |
      Read first: plan sections 3.1 to 3.17, docs/design.md, docs/configuration.md, deploy/kh.example.yaml, tests/unit/test_docs_links.py, tests/unit/test_deploy_files.py.
      Keep every markdown file under 400 lines and every relative link resolving.
      1. `docs/design.md`: rewrite "Access model" and "Claim middleware and call binding" to describe the three credential types (PrivacyFence claim, hub key, OAuth token) and groups from the effective membership (static plus directory snapshot, no restart needed for directory changes); add a section "Sign-in, OAuth and keys" (plan 3.2, 3.7, 3.9, 3.10) and a section "Directory" (3.4 to 3.6, 3.13); rewrite "Deviation from the PrivacyFence contract" to say email is the identity from the hub's own sign-in as well; in the rejected-alternatives table replace the reason of the row "A second auth path" with `Superseded on 2026-11-19 by the owner's decision in issue #58: one middleware now verifies three credential types (design, "Sign-in, OAuth and keys").`, append to the reason of the row "The SDK's `auth=` hook" the sentence `Still true: the hub mounts the SDK's OAuth routes and verifies bearers in its own middleware.`, and add one row per rejected alternative of plan 3.1; add to "Accepted residuals" the four residuals of plan 3.17 and the different-`sub` residual of 3.2.
      2. `docs/configuration.md`: add every variable of plan 3.3 to the tables (a new section "Sign-in and OAuth" and "Directory"), the new `kh.yaml` keys (`legacy_privacyfence`, `directory`, `groups.<g>.directory`) with their validation errors, the commands `kh key` and `kh directory`, and the note that `KH_PF_*` are optional; keep the sentence "The hub never holds admin credentials; `kh setup` does." true by saying the directory sync is a separate process with its own secret files.
      3. `deploy/kh.example.yaml`: add commented examples of `legacy_privacyfence: true`, a `directory:` block (`provider: google`, `domains: [example.com]`) and a `directory:` entry under group `all` with both `google` and `keycloak` references, all commented out so the file still loads and `test_example_config_loads` passes.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py tests/unit/test_deploy_files.py -q passes"
      - "grep -c \"KH_OIDC_ISSUER\" docs/configuration.md prints 1 or more"
      - "grep -n \"A second auth path\" docs/design.md matches a row that names the 2026-11-19 decision"
      - "wc -l docs/design.md docs/configuration.md prints no file above 400 lines"

  - id: p16-docs-retire
    title: Runbook, README, compose example, changelog (retirement phase)
    depends_on: [p14-flow-tests, p15-docs-design-config]
    complexity: M
    touches:
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
      - deploy/compose.example.yaml
      - tests/unit/test_deploy_files.py
    brief: |
      Read first: plan sections 3.14, 3.15 and 4, docs/plans/58/compose-handoff.md, docs/runbook.md, README.md, deploy/compose.example.yaml, tests/unit/test_deploy_files.py, CHANGELOG.md.
      Keep every markdown file under 400 lines and every relative link resolving. This phase edits no other file.
      1. `docs/runbook.md`: add sections "Sign-in and OAuth" (what to set, the public paths, how to test with Claude and Claude Code), "Directory" (Google and Keycloak setup in plan section 4 steps without the console clicks that are the owner's: scopes, roles, the sync service, `kh directory plan`, shrink guard, `--accept-shrink`, stale snapshot alert), "Hub keys" (create, list, revoke, rotate), and "Moving from PrivacyFence" (plan 3.15 steps 1 to 5 with the rollback of each); update the joiners and leavers paragraph: with a provider set, group membership changes in Google or Keycloak and takes effect within about six minutes, `kh person` and `kh group member` still edit static entries; replace the sentence that a restart is needed for directory changes.
      2. `README.md`: add `kh key` and `kh directory` to "The `kh` commands" and one sentence to "Groups" saying members and approvers may come from Google or Keycloak groups; keep the file under 80 lines.
      3. `deploy/compose.example.yaml`: add the environment variables `KH_PUBLIC_URL`, `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID`, `KH_OIDC_ALLOWED_DOMAINS` as comments with placeholder values and `KH_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_oidc_client_secret`; a second service `knowledge-hub-directory` (same image, `command: ["directory", "sync", "--every", "300"]`, networks `[db, egress]`, secrets `pg_kh_indexer` and the directory credential files, `read_only`, `cap_drop: [ALL]`), a `egress` network (not internal) and the secrets `kh_oidc_client_secret`, `kh_directory_google_key`, `kh_directory_keycloak_secret`, all as `{file: /etc/stack-secrets/<name>}`; the file must still contain no secret value. Update `tests/unit/test_deploy_files.py` so the compose example tests expect the new service, network and secrets and still check that no secret value is present.
      4. `CHANGELOG.md` under `## [Unreleased]` (no version heading): `### Added` lines for the OAuth server and consent, the upstream OIDC sign-in with `KH_PUBLIC_URL` and `KH_OIDC_*`, the directory providers `google` and `keycloak` with `kh directory sync|plan|people|show` and the `directory` keys of `kh.yaml`, hub keys with `kh key create|list|revoke`, migration `0006_identity.sql` (run `kh migrate`, then restart `kh serve`), `legacy_privacyfence`; `### Changed` lines for `KH_PF_*` becoming optional, `groups.<g>.approvers` allowed empty with a directory approvers source, `kh setup` reading the effective membership and its new `i account` and `! directory` lines, and `list_audiences` answering from the effective membership; one `### Security` line for the new public surface and the two Google read-only scopes.
      5. Run `.venv/bin/python -m pytest -q` and `.venv/bin/python -m ruff check .` before the last push.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py tests/unit/test_deploy_files.py -q passes"
      - "grep -n \"kh directory\" CHANGELOG.md matches under the Unreleased heading"
      - "grep -n \"knowledge-hub-directory\" deploy/compose.example.yaml matches"
      - "wc -l README.md docs/runbook.md prints no file above 400 lines"
      - ".venv/bin/python -m pytest -q passes"
```
