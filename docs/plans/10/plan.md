# Plan: canonical-repo validator and governance templates

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In a canonical repo's CI, `kh validate --repo R --base B --head H` checks every `.md` file a pull request adds or changes and `kh domain-owners --repo R --pr N` checks that the right owner teams approved it. Each prints one `path: reason` line per problem, exits 0 (pass), 1 (fail) or 2 (error), and posts the commit status `kh/validate` or `kh/domain-owners` itself. `kh governance render --repo R --audience A --out DIR` writes the seed files (schema, `owners.yml`, CODEOWNERS, PR template, workflow) for a repo's first commit.

**Scope:** New package `knowledge_hub.governance`, three new CLI commands, new Forgejo client reads and one guarded status write, a shared `approvals.py` used by the trust walk and by `kh domain-owners`, and a frontmatter schema generated from the parser's constants. The parser's behaviour changes only by forbidding `generated_by` and `derived_from`; the indexer now also skips paths that start with a dot. New config keys `entities.<kind>.reference_audiences` and `governance.owner_teams` (the `cus` default now also allows `mgmt`), and `email_for_visibility` in the PrivacyFence declaration example. Seeding repos, the runner, secrets, labels and branch protection stay in the compose repo.

**Security:** The workflow runs the base branch's definition, never checks out the PR and reads PR content through the API. Messages come from a closed set of texts and never name another repo, file or customer. The status token can write commit statuses, so it is a separate, optional token file; the validator can only post the two fixed contexts.

**Performance:** The id index comes from one tree call per canonical repo and the filename rule, with no file reads and no clones; changed files are read with bounded concurrency.

**Review focus:** the list of rule differences and their decisions (3.2); the no-leak message design (3.6); the shared approval logic and team check (3.8); the status-context and trigger design after the research findings (3.10); the id index built from paths (3.5).

## 1. Goal

Make the two required checks of every `kh/canonical-*` repo exist, built on the hub's own parser and trust rules, so a pull request can be merged and a document can become T0. Tracking issue: [tkcs-lab/knowledge-hub#10](https://github.com/tkcs-lab/knowledge-hub/issues/10). Owner decisions taken in the planning ask (2026-11-16): the content-tree layout is proposed by this plan (there is no tree yet), and the commands post their own commit statuses with an optional `--status-token-file`.

## 2. Current state

- `src/knowledge_hub/indexer/frontmatter.py:11-22`: `FORBIDDEN_KEYS` (8 keys), `REQUIRED_KEYS = (id, title, domain, review_by)`, `ALLOWED_STATUS = (current, superseded)`; `parse_document` (`:76`) ignores unknown keys, takes any string `id`, any string `type` and `owner`, and rejects `status: disputed`.
- `src/knowledge_hub/indexer/trust.py:90`: `_approvals(pr, reviews, bot_logins)` is the one definition of a counted approval (APPROVED, official, not dismissed, `commit_id` equals the PR head, author and bots excluded). `sync_plan.py:23` holds `BOT_LOGINS = ("kh-hub-bot",)`. No code checks team membership.
- `sync_plan.py:250-252` indexes every `.md` blob of the tree, so `.forgejo/pull_request_template.md` and any root `README.md` would raise "missing frontmatter" alerts. `DUPLICATE` (`:28`) names repo and paths and `BAD_REFERENCE` (`:27`) requires supersedes targets in the same repo; both stay operator-only text.
- `forgejo/client.py`: async reads (`head_sha`, `list_tree`, `get_raw`, `pull`, `pull_reviews`, `changed_files`), writes guarded by `_guard_ref` (only `bot/promote/` refs). No compare-with-status, no team or membership read, no commit-status write.
- `grist/client.py`: `fetch_table(name)` returns the `fields` of every record. `cli.py:14` lists commands; `config.py:52-60` has the entity kinds (`cus` audiences `["contracts"]`).
- `pyproject.toml:37` ships only `migrations/*.sql` as package data. `Dockerfile` installs the wheel and sets `ENTRYPOINT ["kh"]`.
- `config_hash` (`indexer/rebuild.py:40`) covers only raw map, repo audiences and entity audiences, so new config fields do not change it (`tests/unit/test_config.py:85` proves it).
- The compose repo's `docs/knowledge-hub.md` section 4 rules differ from the hub; section 3.2 lists every difference. Branch protection there applies the contexts `kh/validate` and `kh/domain-owners`, `kh-ci` has read-only scopes, and `README.md` is created by `auto_init` in each repo.
- Research r-20261116-1 (medium confidence, 2026-11-16): `pull_request_target` and `workflow_dispatch` are documented for Forgejo v15 ([reference](https://forgejo.org/docs/v15.0/user/actions/reference/)); `pull_request_target` runs the workflow from the base repo's default branch; `pull_request_review` is not documented and the status-creating source handles no review events ([commit_status.go](https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/services/actions/commit_status.go), branch not pinned to v15); a job's status context is `<workflow> / <job> (<event>)`; creating a status needs `write:repository`, reading team members needs `read:organization`, and a token limited to selected repositories cannot hold the organization scope ([token scopes](https://forgejo.org/docs/v15.0/user/token-scope/)).

## 3. Design

### 3.1 Module map

All new code is under `src/knowledge_hub/governance/` except `approvals.py`:

| Module | Content | I/O |
|---|---|---|
| `knowledge_hub/approvals.py` | `BOT_LOGINS`, `current_approvals` | none |
| `governance/owners.py` | `OwnersFile`, `Rule`, `load_owners`, `build_owners`, `glob_to_regex`, `render_codeowners` | none |
| `governance/messages.py` | `Code`, `Finding`, `render_line` | none |
| `governance/rules.py` | layout, `id_from_path`, `validate_files` | none |
| `governance/owners_check.py` | `check_owners` | none |
| `governance/schema.py` | `build_schema` | none |
| `governance/render.py` | `render_governance` | files |
| `governance/ci.py` | `run_validate`, `run_domain_owners`, `CheckResult` | Forgejo, Grist |
| `governance/templates/` | `pull_request_template.md`, `kh.yml.tmpl` | package data |

Rejected: putting the gates in `indexer/` (it would mix PR gates with code that needs the database), and a separate CI distribution (two copies of the rules would drift, which is the problem this change removes).

### 3.2 Differences between docs/knowledge-hub.md section 4 and the hub, with decisions

The hub is the source of truth. Every row is a decision; "parser" rows change `parse_document`, "gate" rows live only in `kh validate`.

| # | Compose doc says | Hub does today | Decision |
|---|---|---|---|
| D1 | `title` not listed | `title` required | Keep. Reject the doc's omission: the schema requires `title`. |
| D2 | `status: draft \| current \| superseded \| disputed` | only `current`, `superseded`; `disputed` refused | Keep the hub. `draft` is rejected (an open PR is the draft) and `disputed` is server-assigned; the gate shows the parser's own message. |
| D3 | `type` enum tied to the path | any optional string | Parser unchanged. The gate requires `type` and ties it to the path (table in 3.3). |
| D4 | `owner` is a team equal to the folder's owner team | any optional string | Parser unchanged. The gate requires `owner` to equal the domain's owner team from `owners.yml` on the base commit. |
| D5 | forbidden also `generated_by`, `derived_from` | forbids 8 other keys | Change the parser: add both keys to `FORBIDDEN_KEYS` (the server writes provenance; a human must not). The schema lists all 10. |
| D6 | `additionalProperties: false` | unknown keys ignored | Parser stays tolerant (a stricter indexer could drop live T0 documents on upgrade). The gate rejects keys outside `ALLOWED_KEYS`, so CI is stricter than the indexer, never looser. |
| D7 | `id` matches `^[a-z0-9-]+$` | any non-empty string | Parser unchanged. The gate requires `^[a-z0-9]+(-[a-z0-9]+)*$`. |
| D8 | filename is `<id>.md`; ADR id is `adr-<component-id>-NNNN` | no rule | Gate only (3.3). |
| D9 | `glossary.md` has `type: glossary`, `domain: portfolio`, edits owned by `kh-admins` | no rule | Gate only; `owners.yml` maps it to `kh-admins`. |
| D10 | a superseded target must be `status: superseded` after the merge | the indexer sets `superseded` from the pointer | Reject the rule: the server assigns it. The gate keeps the indexer's rule: targets exist in the same repo and are not the file itself. |
| D11 | entity regex fixed to `prd\|cmp\|ver\|cus`; `cus:` only in contracts and mgmt | kinds are configurable; `cus` audiences `["contracts"]` | Change config: the regex is built from `config.entities`; new optional `reference_audiences` per kind says where documents may cite it; default for `cus` is `["contracts", "mgmt"]`. |
| D12 | mapping file `schema/owners.yml` | none | Follow the change request: `owners.yml` at the repo root, `.forgejo/CODEOWNERS` generated from it. |
| D13 | id unique across all `kh/*` repos | duplicates rejected per repo | Keep the indexer's rule and add the cross-repo rule in the gate only. |
| D14 | governance files live in the repo | every `.md` blob is indexed | Change the indexer: paths with a dot-prefixed segment are skipped, so `.forgejo/pull_request_template.md` raises no alert. A root `README.md` still raises an alert; the compose repo should not seed one. |
| D15 | `pull_request_review` re-runs `kh/domain-owners` | n/a | Not documented in Forgejo v15 (research). The template omits the trigger by default; the fallback is `workflow_dispatch` called by an n8n workflow (3.10). |
| D16 | status named `kh/validate` | n/a | A job's context is `<workflow> / <job> (<event>)`, so the commands post the status themselves (3.10). |

### 3.3 Layout and types (proposal, no tree existed)

A document path is one of:

- `glossary.md` at the root: `type: glossary`, `domain: portfolio`, id `glossary`.
- `<domain>/<product-id>/<kind>/<name>.md`: a product-level document.
- `<domain>/<product-id>/<component-id>/<kind>/<name>.md`: a component-level document.
- `<domain>/<product-id>/<component-id>/adr/NNNN-<slug>.md`: an ADR (component-level only).

`<domain>` is one of `config.domains`. `<kind>` is one of the keys of `TYPE_FOLDERS = {"requirements": "requirement", "specs": "spec", "guides": "guide", "adr": "adr"}`; the value is the required `type`. `<product-id>` and `<component-id>` match `^[a-z0-9]+(-[a-z0-9]+)*$` and a `<component-id>` is never a `<kind>` name. For a normal file `<name>` is the id, so `id == <name>`; for an ADR the filename matches `^\d{4}-[a-z0-9]+(-[a-z0-9]+)*\.md$` and `id == "adr-<component-id>-NNNN"`. Markdown files whose path has a dot-prefixed segment (`.forgejo/...`) are not documents and are skipped by the gate. Any other `.md` path fails with `Code.LAYOUT`.

### 3.4 Constants and parser change

`frontmatter.py`: `FORBIDDEN_KEYS` gains `"generated_by", "derived_from"`; new `OPTIONAL_KEYS = ("type", "owner", "status", "entities", "supersedes", "superseded_by")`; `ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS`; new `raw_keys(text: str) -> list[str] | None` returns the frontmatter mapping's keys or `None` when the block is missing or not a mapping. `parse_document` behaviour is otherwise unchanged. The schema, the gate and the schema test all use these constants.

### 3.5 The id index (kh validate)

Every document's id follows from its path (3.3), so the index of a repo is `{id_from_path(p) for p in tree}`; no file is read. For each `config.repos` entry with tier `T0` the run calls `head_sha(repo, "main")` and `list_tree` once (all repos concurrently; a repo the token cannot read, HTTP 404, is skipped). The PR's own repo uses the tree at `--head`; other repos use `main`. A document id is "in use" when another path in the head tree of its repo maps to it, or when any other repo's index holds it. The index is built once per run and held in memory. Legacy files that break the filename rule are not in the index; the indexer's `DUPLICATE` rule is the backstop. Two open PRs that add the same id are caught at the second PR's next run or by the indexer.

### 3.6 Rules, messages and no-leak design

`rules.validate_files(files, *, config, owners, ids_in_repo, ids_elsewhere, entity_keys, repo_audience) -> list[Finding]`, pure. Per file, in this order: layout (3.3); `parse_document(text, path, config.domains)` (a `Rejected` becomes one `Code.PARSE` finding carrying its reason, then the file is done); then `raw_keys` against `ALLOWED_KEYS`; then domain equals layout domain; id format and id equals the path's id; `type` present and equal to the path's type; `owner` present and equal to `owners.domains[domain]`; entities; folder entities; supersedes; id uniqueness. Entities: each must match `^(<kinds>):[a-z0-9.-]+$`; a kind whose `reference_audiences` does not contain `repo_audience` fails with `Code.ENTITY_NOT_ALLOWED` before any lookup, so the answer never depends on whether the entity exists; otherwise it must be in `entity_keys[kind]` or fails with `Code.ENTITY_NOT_FOUND`. The `<product-id>` folder must exist as `prd:<product-id>` and the `<component-id>` folder as `cmp:<component-id>`.

`messages.Code` is a closed enum; each member has a fixed text and at most one placeholder filled from the file's own frontmatter or path (a key name, an expected value, an entity reference the author wrote). No text takes a repo name, a path or a value from another repo or from Grist. Texts: `LAYOUT` "path does not match the repository layout", `PARSE` "<parser reason>", `UNKNOWN_KEY` "unknown frontmatter key <key>", `DOMAIN` "domain must be <domain>", `ID_FORMAT` "id must use lowercase letters, digits and single hyphens", `ID_FILENAME` "id must be <expected>", `ID_IN_USE` "id already in use", `TYPE_MISSING` "type is required", `TYPE_PATH` "type must be <type> for this path", `OWNER_MISSING` "owner is required", `OWNER` "owner must be <team>", `ENTITY_FORMAT` "entity <ref> is not a valid reference", `ENTITY_NOT_ALLOWED` "entity <ref> is not allowed in this repository", `ENTITY_NOT_FOUND` "entity not found: <ref>", `FOLDER_UNKNOWN` "product or component folder is not a known entity", `SUPERSEDES` "supersedes and superseded_by must name another document of this repository", `TOO_LARGE` "file is larger than 1 MiB", `TOO_MANY` "more than 200 markdown files changed". A line is `<path>: <text>`; the path is the file in this PR. Findings are sorted by path then code. The no-leak test (Phase p6) runs the command against fake repos that hold the colliding id and entity and asserts that no output byte contains the other repo's name, its file paths, or the Grist customer key.

### 3.7 kh validate (exact behaviour)

`kh validate --repo NAME --base SHA --head SHA [--forgejo-url U] [--forgejo-org O] [--token-file F] [--status-token-file F] [--grist-url U] [--grist-key-file F] [--grist-doc-id D] [--config F] [--no-status]`. Settings fill missing values: `KH_FORGEJO_URL`, `KH_FORGEJO_ORG` (default `kh`), `KH_GRIST_URL`, `KH_GRIST_KEY_FILE`, `KH_GRIST_DOC_ID`, `KH_CONFIG_FILE`; the token file is required (flag only). Steps: load config; read `owners.yml` at `--base` (missing or invalid: error); compare `base...head` (`compare_files`), keep `.md` files whose status is not `removed` and whose path has no dot-prefixed segment; read each at `--head` (8 at a time); build the index (3.5); fetch only the Grist tables the run needs (the kinds cited plus `prd` and `cmp` when folders are checked), concurrently; run `validate_files`; print lines; post the status unless `--no-status` (the status token file when given, else the read token): state `success` with description "all checks passed", `failure` with "<n> problems", `error` with "check could not run". Exit 0 pass, 1 fail, 2 error (message on stderr, no secrets, no response bodies). Files over 1 MiB or more than 200 changed `.md` files fail with `TOO_LARGE`/`TOO_MANY` (exit 1).

### 3.8 owners.yml, approvals and kh domain-owners

`owners.yml` (root):

```yaml
version: 1
audience: all
domains: {portfolio: owners-portfolio, product: owners-product, technical: owners-technical}
rules:            # first match wins
  - {team: kh-admins, paths: [".forgejo/**", "schema/**", "owners.yml", "CODEOWNERS", "glossary.md"]}
  - {team: owners-portfolio, paths: ["portfolio/**"]}
  - {team: owners-product, paths: ["product/**"]}
  - {team: owners-technical, paths: ["technical/**"]}
  - {team: kh-admins, paths: ["**"]}
```

For a restricted repo `domains` maps all three domains to `owners-<aud>` and the rules are the governance rule to `kh-admins` followed by `{team: owners-<aud>, paths: ["**"]}`. Glob syntax: `**` matches any characters including `/`, `*` any characters except `/`, `?` one character except `/`; matching is on the full path.

`approvals.current_approvals(pr, reviews, bot_logins)` is the body of today's `_approvals` and the only definition of a counted approval; `trust.py` and `kh domain-owners` both call it. `kh domain-owners --repo NAME --pr N [--forgejo-url U] [--forgejo-org O] [--token-file F] [--status-token-file F] [--config F] [--no-status]` steps: read the PR; if it is not open print `pull request is not open` and exit 0 without a status; read `owners.yml` at `pr.base.sha`; compare `merge_base` (else `base.sha`) to `head.sha`, taking both `filename` and `previous_filename` of each file; map each path to the team of the first matching rule (a path with no rule fails); read the reviews; keep `current_approvals(pr, reviews, BOT_LOGINS)`; a rule is satisfied when some counted reviewer is a member of its team (`is_team_member`, cached). Output lines: `<pattern>: approval from <team> required`, one per unsatisfied rule, using the rule's first pattern that matched a touched path. Widening: a label `widen-audience/<aud>` (with `<aud>` in `config.audiences`) adds the requirement of one counted approval from a member of any team in `owner_teams_for(config, aud)`, and requires the PR body to hold a line `Reason for widening: <text>` with real text (a `<` at the start of the text counts as the placeholder and fails); a bare `widen-audience` label fails with `widen-audience needs a destination label widen-audience/<audience>`. Status context `kh/domain-owners` on `head.sha`. A reviewer who is a member of two teams counts for both. A stale approval (commit id not the current head), a dismissed or non-official review, the author and `kh-hub-bot` never count.

`config.py` gains `GovernanceConfig(owner_teams={})` as `Config.governance` and `owner_teams_for(config, audience)`: the configured list when present, else `["owners-portfolio", "owners-product", "owners-technical"]` for `all`, else `["owners-<audience>"]`; team names match `^[a-z0-9][a-z0-9_-]{0,62}$` and `owner_teams` keys must be in `audiences`.

### 3.9 Forgejo client additions

`compare_files(repo, base, head) -> list[dict]` (`filename`, `status`, `previous_filename` when present); `head_sha_or_none(repo, branch)` (HTTP 404 gives `None`); `team_id(name) -> int | None` (pages through `GET /orgs/{org}/teams`); `is_team_member(team_id, login) -> bool` (`GET /teams/{id}/members/{login}`: 204/200 yes, 404 no, other statuses raise); `post_status(repo, sha, context, state, description)` posts `POST /repos/{org}/{repo}/statuses/{sha}` with `{context, state, description}` after `_guard_context(context)` accepts only `kh/validate` and `kh/domain-owners` and `state` is one of `success|failure|error|pending`. Errors keep the fixed texts of the module (method, path, status; never body or token). Required scopes, to be passed to the compose repo: read token `read:repository` + `read:organization` with unrestricted repositories; status token `write:repository`.

### 3.10 Workflow, contexts and triggers

The commands post the two contexts themselves, so the workflow's own derived contexts (`kh / validate (pull_request_target)`) are extra and not required by branch protection. The template `.forgejo/workflows/kh.yml` (workflow name `kh`; jobs `validate` and `domain-owners`; `runs-on: kh-validate`, the runner label `kh-validate:host`) has the triggers `pull_request_target` (`opened`, `synchronize`, `reopened`) and `workflow_dispatch` (input `pr`). `pull_request_review` is rendered only with `kh governance render --review-trigger`, default off, because Forgejo v15 does not document it and creates no status for it; before anyone turns it on the live probe (manual step ma1) must show that it reads the base branch's workflow. The fallback for review events: an n8n workflow on the Forgejo review webhook that calls `POST /repos/{org}/{repo}/actions/workflows/kh.yml/dispatches` with `ref` `main` and `inputs.pr`, so `kh domain-owners` (not n8n) computes the status and the approval logic exists once. Rejected: an n8n workflow that sets the status itself (a second copy of the approval and team rules). Steps never use a checkout, never interpolate PR fields into `run:` (values pass through `env:`), write the token to a `mktemp` file with `umask 077` and delete it on exit, and pass `--status-token-file` only when the secret `KH_CI_STATUS_TOKEN` is non-empty. Secrets: `KH_CI_TOKEN`, `KH_CI_GRIST_KEY`, optional `KH_CI_STATUS_TOKEN`; variables `KH_GRIST_URL`, `KH_GRIST_DOC_ID`, optional `KH_FORGEJO_URL` (default `github.server_url`).

### 3.11 Templates and render

`kh governance render --repo NAME --audience A --out DIR [--org kh] [--portfolio-team owners-portfolio] [--product-team owners-product] [--technical-team owners-technical] [--owner-team owners-A] [--admin-team kh-admins] [--review-trigger]` writes, under `DIR`: `schema/frontmatter.schema.json`, `owners.yml`, `.forgejo/CODEOWNERS`, `.forgejo/pull_request_template.md`, `.forgejo/workflows/kh.yml`. Audience `all` uses the three domain teams; any other audience uses `--owner-team` (default `owners-<A>`). Output is deterministic (sorted keys, trailing newline). The schema is JSON Schema 2020-12: `properties` for every `ALLOWED_KEYS` member (`id` pattern, `domain` enum from the default domains, `type` enum from `TYPE_FOLDERS` values plus `glossary`, `status` enum `ALLOWED_STATUS`, `entities` items pattern for the default kinds, `review_by` string with `format: date`), `required` = `REQUIRED_KEYS` plus `type`, `additionalProperties: false`, and `x-forbidden-keys` = `FORBIDDEN_KEYS`. CODEOWNERS lists one line per rule path in reverse rule order (last match wins in the GitHub syntax) as `<pattern> @<org>/<team>`; it is advisory because the CI check is authoritative and Forgejo's CODEOWNERS enforcement is unprobed. The PR template has the headings `## Claim`, `## Sources` (ids with a quote or timestamp), `## Affected documents`, `## Confidence`, `## Audience check` (a checkbox "This change does not widen the audience", and the line `Reason for widening: <only for widen-audience/<audience> PRs>`). Templates ship as package data (`governance/templates/*`) and the Docker image needs no other change because it installs the wheel.

### 3.12 PrivacyFence declaration and docs

Add `email_for_visibility: true` to the `kh` upstream in `deploy/privacyfence-upstream.example.yaml`; in `docs/design.md` mark the email deviation resolved and cite PrivacyFence ADR 0310 (opt-in key shipped in 8.0.0a3); remove the "Blessing email matching" item from the runbook's PrivacyFence follow-ups. The key's exact placement in PrivacyFence's loader is not verified here (see Risks).

## 4. ADRs

None (this repository has no ADR directory); the decisions live in sections 3.2 and 3.10 and in `docs/governance.md`.

## 5. Manual steps

### Before implementation

None.

### After implementation (before the feature pull request merges)

**1. ma1-live-probe: check the live Forgejo (about 30 minutes, needs the compose repo's seeded repos).**
1. Open a test PR in a seeded canonical repo that changes one document and confirm two statuses named exactly `kh/validate` and `kh/domain-owners` appear on the head commit (needs `KH_CI_STATUS_TOKEN` with `write:repository`).
2. Approve it as a member of the matching owner team and run `kh.yml` by hand with `workflow_dispatch` and `pr` set; confirm `kh/domain-owners` turns green.
3. Render with `--review-trigger` into a scratch branch only and submit a review: confirm whether a run starts and whether the workflow file is read from the base branch. Report the finding; do not leave the trigger on unless the base branch's file is used.
4. Start PrivacyFence with the edited declaration and confirm it accepts `email_for_visibility: true` on the `kh` upstream.
Report back: pass or fail for each of the four steps.
Compare truncation check: in step 1 use a test PR that changes more than 50 markdown files and confirm every one is reported.

### After the feature pull request merges (outside the manifest)

**Release 0.2.0 (about 10 minutes).** The release must contain this change, so it runs after the merge.
1. In a Claude Code session on this repository run `/tld:release` and give version `0.2.0`; approve the one board question it asks.
2. When the release workflow ends, read the published image's `sha256:` digest from the workflow summary.
3. Edit the description of the merged feature pull request: add the line `Image digest: <digest>`.
Report back: pass or fail, and the digest.

## 6. Risks and open questions

- **Review events (high).** Forgejo v15 documents no `pull_request_review` trigger and its status source ignores review events; unproven which workflow file such a run reads. Mitigation: trigger off by default, the `workflow_dispatch` fallback, probe in ma1. If a phase finds the workflow template cannot keep to "no PR code", it stops with `status=blocked`.
- **Status token power (high).** `write:repository` also allows pushing branches. Mitigation: a separate optional token, the client posts only two contexts, and branch protection blocks `main`. The compose repo should give it to a dedicated account.
- **Destination owners and `official` (medium).** A destination team's approval counts only when Forgejo marks it official on the source repo, so the compose repo must put the destination owner teams on the approvals whitelist of repos that can be widened from, or the check can never pass. Documented in `docs/governance.md`.
- **Label names (medium).** `widen-audience/<audience>` labels must exist; the compose repo creates them.
- **Compare truncation (medium).** If Forgejo's compare lists fewer files than changed, a file could be unchecked. Mitigation: the live probe (ma1, step 1 note) uses a PR with more than 50 changed files.
- **Id index from paths (medium).** Files breaking the filename rule are invisible to it; the indexer's `DUPLICATE` rule is the backstop.
- **PrivacyFence key placement (medium).** The `email_for_visibility` key is placed beside `identity_claim` in the `kh` upstream from the request's wording only; PrivacyFence's loader is not readable here, so ma1 step 4 verifies it.
- **Root README (low).** `auto_init` creates `README.md`; the indexer raises an alert for it and the gate fails any PR touching it. Reported to the compose repo.
- **CODEOWNERS syntax (low).** Forgejo's CODEOWNERS matching is unprobed; the file is advisory.

## Implementation manifest

```yaml
plan_slug: canonical-validator
feature_branch: feature/canonical-validator
tracking_issue: 10
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-live-probe
    title: Check the statuses, the dispatch fallback, the review trigger and the PrivacyFence key on the live Forgejo
    why: CI cannot reach the live Forgejo or PrivacyFence, and Forgejo's review-trigger behaviour is undocumented.
verify_after_merge:
  - .venv/bin/python -m pytest -q
final_checks:
  - "`tld plan deviations check --file docs/plans/10/deviations.md --plan docs/plans/10/plan.md --all` exits 0"
  - "the changelog has entries under docs.changelog.section and no version heading"
phases:
  - id: p1-shared-core
    title: Forbid provenance keys, share approvals, skip dot paths in the indexer
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/approvals.py
      - src/knowledge_hub/indexer/trust.py
      - src/knowledge_hub/indexer/sync_plan.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_approvals.py
      - tests/unit/test_sync_plan.py
    brief: |
      Implement design 3.4 and the shared approvals of 3.8, and decision D14.
      1. In `src/knowledge_hub/indexer/frontmatter.py` append `"generated_by"` and `"derived_from"` to `FORBIDDEN_KEYS`; add `OPTIONAL_KEYS = ("type", "owner", "status", "entities", "supersedes", "superseded_by")` and `ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS` below `REQUIRED_KEYS`; add `def raw_keys(text: str) -> list[str] | None` that calls `_split`, loads the block with `yaml.safe_load` inside `try/except (yaml.YAMLError, ValueError)` and returns `list(data)` (keys as given) when `data` is a dict, else `None`. Do not change any existing behaviour of `parse_document`.
      2. Create `src/knowledge_hub/approvals.py` with the module docstring "The one definition of a counted approval", `BOT_LOGINS: tuple[str, ...] = ("kh-hub-bot",)` and `current_approvals(pr, reviews, bot_logins)` whose body is exactly that of `_approvals` in `trust.py:90-110` and whose `bot_logins` parameter is typed `Collection[str]` (import from `collections.abc`).
      3. In `trust.py` delete `_approvals`, add `from knowledge_hub.approvals import current_approvals` and call it in `_merge_verdict` (`trust.py:130`).
      4. In `sync_plan.py` delete the `BOT_LOGINS` line (`:23`), add `from knowledge_hub.approvals import BOT_LOGINS`, and in the tree loop at `:250-252` skip an entry when any segment of `path.split("/")` starts with `"."` (add the condition to the existing `continue`).
      5. Tests: in `tests/unit/test_frontmatter.py` extend the parametrised forbidden-key test with `generated_by` and `derived_from`, and add a test that `ALLOWED_KEYS` has no member in `FORBIDDEN_KEYS` and that `raw_keys` returns the keys, returns `None` for text without frontmatter and for a list frontmatter. Create `tests/unit/test_approvals.py` with one test per exclusion (not APPROVED, not official, dismissed, other commit id, author, bot login) and one that counts a good review; copy the review shape from `tests/support/forgejo_fake.py:approval`. In `tests/unit/test_sync_plan.py` extend a tree test so a `.forgejo/x.md` blob produces no alert and no document; copy the closest existing test's fixture style.
      Do not touch `docs/` or `CHANGELOG.md`. Stop with `status=blocked` if any existing test in `tests/unit/test_trust.py` or `tests/unit/test_sync_plan.py` fails after step 3 or 4.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_approvals.py tests/unit/test_trust.py tests/unit/test_sync_plan.py -q passes"
      - "grep -rn \"_approvals\" src tests returns nothing"
      - "grep -n \"BOT_LOGINS\" src/knowledge_hub/indexer/sync_plan.py shows only the import and the use"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-config-owners
    title: Governance config, owners.yml model and CODEOWNERS rendering
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/governance/__init__.py
      - src/knowledge_hub/governance/owners.py
      - tests/unit/test_config.py
      - tests/unit/test_governance_owners.py
    brief: |
      Implement the config part of D11 and design 3.8 (owners.yml) and the CODEOWNERS part of 3.11.
      1. `config.py`: add `reference_audiences: list[str] | None = None` to `EntityKind`; in `_default_entities` give `cus` `reference_audiences=["contracts", "mgmt"]` (the others keep `None`); in `_legacy_entity_lists` add `"reference_audiences": ["contracts", "mgmt"]` to the dict built for kind `cus`; in `Config._validate` raise `ValueError(f"entities {kind} has unknown reference audience {a!r}")` for a member not in `known`. Add `class GovernanceConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`, `owner_teams: dict[str, list[str]] = {}`, a `TEAM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")` check on every team name, and `Config.governance: GovernanceConfig = GovernanceConfig()`; in `Config._validate` raise `ValueError(f"governance owner_teams has unknown audience {a!r}")` for a key not in `known`. Add `def owner_teams_for(config: Config, audience: str) -> list[str]` as specified in 3.8.
      2. Create `governance/__init__.py` with a docstring and `TYPE_FOLDERS = {"requirements": "requirement", "specs": "spec", "guides": "guide", "adr": "adr"}` (phases p3 and p5 import it from `knowledge_hub.governance`), and `governance/owners.py` with: `GOVERNANCE_PATHS = (".forgejo/**", "schema/**", "owners.yml", "CODEOWNERS", "glossary.md")`; pydantic models `Rule(team: str, paths: list[str])` and `OwnersFile(version: Literal[1], audience: str, domains: dict[str, str], rules: list[Rule])` with `extra="forbid"`; `load_owners(text: str) -> OwnersFile` using `yaml.safe_load` and raising `OwnersError(ValueError)` with a message starting `owners.yml:` on invalid YAML or a validation error; `glob_to_regex(pattern: str) -> re.Pattern[str]` as the glob syntax of 3.8 (`**` to `.*`, `*` to `[^/]*`, `?` to `[^/]`, everything else escaped, full match); `OwnersFile.rule_for(path) -> Rule | None` (first match); `build_owners(audience: str, *, portfolio_team: str, product_team: str, technical_team: str, owner_team: str, admin_team: str) -> OwnersFile` giving exactly the two shapes of 3.8 (audience `all`: the three domain rules plus the final `kh-admins` catch-all; any other audience: governance rule then `owner_team` with `["**"]` and all three domains mapped to `owner_team`); `dump_owners(o: OwnersFile) -> str` (YAML with the keys in the order `version, audience, domains, rules`, rule keys `team, paths`, block style, trailing newline, written by `yaml.safe_dump(..., sort_keys=False)`); `render_codeowners(o: OwnersFile, org: str) -> str` writing a header comment line `# Generated from owners.yml; the CI check is authoritative.` then, for the rules in reverse order and each path in order, `<pattern> @<org>/<team>` where a pattern `x/**` becomes `/x/`, `**` becomes `*`, and any other pattern gets a leading `/`.
      3. Tests: `tests/unit/test_config.py` gets tests for the `cus` default, the legacy list form, unknown reference audience, `owner_teams_for` for `all`, `hr` and a configured override, and that `config_hash` is unchanged (the existing test already covers it; keep it passing). `tests/unit/test_governance_owners.py` covers `load_owners` errors, `glob_to_regex` on `portfolio/**`, `owners.yml`, `*.md` vs `a/b.md`, first-match order, both `build_owners` shapes, `dump_owners` round trip through `load_owners`, and `render_codeowners` for the `all` shape (assert the exact text).
      Stop with `status=blocked` if `config_hash` for `Config()` changes.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_governance_owners.py -q passes"
      - "grep -n \"reference_audiences\" src/knowledge_hub/config.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-forgejo-client
    title: Forgejo compare, team, membership and guarded status methods
    depends_on: []
    complexity: S
    touches:
      - src/knowledge_hub/forgejo/client.py
      - tests/unit/test_forgejo_status.py
    brief: |
      Implement design 3.9.
      1. In `forgejo/client.py` add the constants `STATUS_CONTEXTS = ("kh/validate", "kh/domain-owners")` and `STATUS_STATES = ("success", "failure", "error", "pending")` and `def _guard_context(context: str, state: str) -> None` raising `ForgejoRefused("write refused: not a kh status")` unless both are allowed.
      2. Add to `ForgejoClient`: `async def compare_files(self, repo, base, head) -> list[dict[str, Any]]` (same GET as `changed_files`, returning dicts with `filename`, `status` and `previous_filename` when the API gives it; keep `changed_files` unchanged); `async def head_sha_or_none(self, repo, branch) -> str | None` (GET the branch like `head_sha`, HTTP 404 returns `None`, other non-200 raises `ForgejoError`; copy the 404 handling of `path_exists`); `async def team_id(self, name: str) -> int | None` (page through `GET /orgs/{org}/teams` with `page` and `limit=50` until a short page, return the `id` of the team whose `name` equals `name`, else `None`); `async def is_team_member(self, team_id: int, login: str) -> bool` (GET `/teams/{team_id}/members/{login}` with `_q(login)`; 200 or 204 gives `True`, 404 gives `False`, anything else raises `ForgejoError`; use `self._http.get` directly as `path_exists` does because `_get` accepts only 200); `async def post_status(self, repo, sha, context, state, description) -> None` that calls `_guard_context`, then `self._send("POST", f"{self._repo(repo)}/statuses/{_q(sha)}", {"context": context, "state": state, "description": description[:140]})`.
      3. Update the module docstring first paragraph to say that status posts are guarded by `_guard_context` instead of a ref.
      4. Create `tests/unit/test_forgejo_status.py` using the `make`/`js` helpers copied from `tests/unit/test_forgejo_client.py:10-25` (import them from there is not possible; copy the six lines). Tests: `compare_files` returns the dicts and calls the compare URL; `head_sha_or_none` returns `None` on 404 and the sha on 200 and raises on 500; `team_id` finds a team on the second page; `is_team_member` for 204, 404 and 500; `post_status` sends the exact JSON body and path and never puts the token in the body; `post_status` with another context or state raises `ForgejoRefused` and sends no request; an error message from a failing `post_status` contains no token.
      Stop with `status=blocked` if the existing tests in `tests/unit/test_forgejo_client.py` or `tests/unit/test_forgejo_write.py` fail.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_status.py tests/unit/test_forgejo_client.py tests/unit/test_forgejo_write.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p9-privacyfence-docs
    title: PrivacyFence email_for_visibility and the resolved email deviation
    depends_on: []
    complexity: S
    touches:
      - deploy/privacyfence-upstream.example.yaml
      - docs/design.md
      - docs/runbook.md
      - tests/unit/test_privacyfence_declaration.py
    brief: |
      Implement design 3.12.
      1. In `deploy/privacyfence-upstream.example.yaml` add the line `    email_for_visibility: true` directly under `identity_claim: required` (same indentation as that key) and add one sentence to the header comment: `email_for_visibility lets PrivacyFence put the verified email in the claim for this upstream (PrivacyFence 8.0.0a3, ADR 0310); without it, meetings that are not mapped to an audience are visible to nobody.`
      2. In `tests/unit/test_privacyfence_declaration.py` add `"email_for_visibility"` to the `allowed` set in `test_upstream_fields` (`:63-66`) and add a test that `UPSTREAM["email_for_visibility"] is True`.
      3. In `docs/design.md` rewrite the section "Deviation from the PrivacyFence contract" (`docs/design.md:91-99`): keep the first paragraph's facts, change the heading to "Deviation from the PrivacyFence contract (resolved)", replace the "Risks ... Ask PrivacyFence to:" list by two sentences saying that PrivacyFence 8.0.0a3 ships the opt-in key `email_for_visibility` (PrivacyFence ADR 0310) and that the declaration sets it, and keep one sentence that a change of how PrivacyFence fills `email` changes who sees attendee-only rows.
      4. In `docs/runbook.md` delete the bullet "Blessing email matching." from "PrivacyFence follow-ups" (`:128`) and append the sentence `The example declaration already sets \`email_for_visibility: true\`.` to step 8 of the install order (`:23`).
      Stop with `status=blocked` if any other assertion of `tests/unit/test_privacyfence_declaration.py` fails after the edit.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_privacyfence_declaration.py tests/unit/test_deploy_files.py tests/unit/test_docs_links.py -q passes"
      - "grep -n \"email_for_visibility: true\" deploy/privacyfence-upstream.example.yaml matches"
      - "grep -n \"Blessing email matching\" docs/runbook.md returns nothing"
  - id: p3-schema-render
    title: Frontmatter schema, templates and kh governance render
    depends_on: [p1-shared-core, p2-config-owners]
    complexity: M
    touches:
      - src/knowledge_hub/governance/schema.py
      - src/knowledge_hub/governance/render.py
      - src/knowledge_hub/governance/templates/pull_request_template.md
      - src/knowledge_hub/governance/templates/kh.yml.tmpl
      - src/knowledge_hub/cli.py
      - pyproject.toml
      - tests/unit/test_governance_schema.py
      - tests/unit/test_governance_render.py
      - tests/golden/canonical-all/**
      - tests/golden/canonical-hr/**
    brief: |
      Implement design 3.10 (the workflow file) and 3.11. `TYPE_FOLDERS` comes from `knowledge_hub.governance` (phase p2).
      1. `governance/schema.py`: `build_schema() -> dict` exactly as the schema paragraph of 3.11 (`$schema` `https://json-schema.org/draft/2020-12/schema`, `title` "knowledge-hub frontmatter", `type` object; `properties` keyed by `ALLOWED_KEYS` from `knowledge_hub.indexer.frontmatter`: `id` string with pattern `^[a-z0-9]+(-[a-z0-9]+)*$`; `title` string minLength 1; `domain` enum `["portfolio","product","technical"]`; `review_by` string with `format: date`; `type` enum `sorted(set(TYPE_FOLDERS.values()) | {"glossary"})`; `owner` string; `status` enum `list(ALLOWED_STATUS)`; `entities` array of strings with pattern `^(prd|cmp|ver|cus):[a-z0-9.-]+$`; `supersedes` and `superseded_by` `oneOf` string or array of strings; `required` = `sorted(REQUIRED_KEYS + ("type",))`; `additionalProperties` false; `x-forbidden-keys` = `list(FORBIDDEN_KEYS)`); `dump_schema() -> str` = `json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"`.
      2. Templates: `templates/pull_request_template.md` with the headings and checkbox of 3.11 (under 40 lines); `templates/kh.yml.tmpl` is the workflow of 3.10 as plain YAML with two markers, `{{REVIEW_TRIGGER}}` (replaced by the exact lines `  pull_request_review:` and `    types: [submitted, dismissed]` with a leading comment line `  # Forgejo v15 does not document this trigger; see docs/governance.md.`, or removed with its line when off) and nothing else. Workflow rules: `name: kh`; `on:` `pull_request_target` types `[opened, synchronize, reopened]`, `workflow_dispatch` with input `pr` (type string, required false); job `validate` with `if: github.event_name == 'pull_request_target'`, `runs-on: kh-validate`, one step `run:` of a bash script; job `domain-owners` with `runs-on: kh-validate`, one step. Every PR value enters through the step's `env:` block (`REPO: ${{ github.event.repository.name }}`, `ORG: ${{ github.repository_owner }}`, `BASE: ${{ github.event.pull_request.base.sha }}`, `HEAD: ${{ github.event.pull_request.head.sha }}`, `PR: ${{ github.event.pull_request.number || github.event.inputs.pr }}`, `FORGEJO_URL: ${{ vars.KH_FORGEJO_URL || github.server_url }}`, `GRIST_URL: ${{ vars.KH_GRIST_URL }}`, `GRIST_DOC: ${{ vars.KH_GRIST_DOC_ID }}`, `CI_TOKEN: ${{ secrets.KH_CI_TOKEN }}`, `GRIST_KEY: ${{ secrets.KH_CI_GRIST_KEY }}`, `STATUS_TOKEN: ${{ secrets.KH_CI_STATUS_TOKEN }}`; the `domain-owners` job needs only the token variables) and the script only reads `"$VAR"` (quoted). Script shape: `set -euo pipefail; umask 077; dir=$(mktemp -d); trap 'rm -rf "$dir"' EXIT; printf %s "$CI_TOKEN" > "$dir/token"; ...; extra=(); if [ -n "$STATUS_TOKEN" ]; then printf %s "$STATUS_TOKEN" > "$dir/status"; extra+=(--status-token-file "$dir/status"); fi; kh validate --repo "$REPO" --forgejo-org "$ORG" --base "$BASE" --head "$HEAD" --forgejo-url "$FORGEJO_URL" --token-file "$dir/token" --grist-url "$GRIST_URL" --grist-key-file "$dir/grist" --grist-doc-id "$GRIST_DOC" ${extra[@]+"${extra[@]}"}` (and `kh domain-owners --repo "$REPO" --forgejo-org "$ORG" --pr "$PR" --forgejo-url "$FORGEJO_URL" --token-file "$dir/token" ${extra[@]+"${extra[@]}"}`). No `actions/checkout` anywhere.
      3. `governance/render.py`: `render_governance(out: Path, *, repo: str, audience: str, org: str = "kh", portfolio_team="owners-portfolio", product_team="owners-product", technical_team="owners-technical", owner_team: str | None = None, admin_team="kh-admins", review_trigger=False) -> list[Path]` writing the five files of 3.11 (owner_team defaults to `f"owners-{audience}"`; `owners.yml` from `build_owners` and `dump_owners`; the header comment of `owners.yml` is not written, so output is the dump only), creating directories, returning the written paths sorted, reading templates with `importlib.resources.files("knowledge_hub.governance") / "templates" / name`. Raise `ValueError("render: audience must match ^[a-z0-9][a-z0-9_-]{0,62}$")` for a bad audience.
      4. `cli.py`: add `"governance"` handling: `sub.add_parser("governance")` with a nested subparser `render` taking `--repo`, `--audience`, `--out` (all required), `--org`, `--portfolio-team`, `--product-team`, `--technical-team`, `--owner-team`, `--admin-team` and `--review-trigger` (store_true); it prints one `wrote <relative path>` per file and returns 0, and returns 2 with the message on stderr on `ValueError`. Keep `COMMANDS` for the existing parsers and dispatch `governance` right after `parser.parse_args(argv)`, before the `serve` branch and before the `args.command not in (...)` gate that returns `_not_implemented()` (it needs no settings).
      5. `pyproject.toml`: `package-data` becomes `knowledge_hub = ["migrations/*.sql", "governance/templates/*"]`.
      6. Tests. `test_governance_schema.py`: `properties` keys equal `set(ALLOWED_KEYS)`; `required` equals `set(REQUIRED_KEYS) | {"type"}`; `additionalProperties is False`; `x-forbidden-keys` equals `list(FORBIDDEN_KEYS)`; `status` enum equals `list(ALLOWED_STATUS)`; no key is both allowed and forbidden; and a test that a document dict built from `ParsedDoc` fields for a passing frontmatter uses only schema properties. `test_governance_render.py`: render `canonical-all` (audience `all`) and `canonical-hr` (audience `hr`) into `tmp_path` and compare every file byte for byte with `tests/golden/<repo>/<same relative path>`; regenerate goldens when the environment variable `KH_UPDATE_GOLDEN=1` is set (the test writes the files instead of comparing); a test that `--review-trigger` adds the review lines and the default has none; a test that the workflow text contains no `actions/checkout` and no `${{` inside any line starting with `kh ` or `printf`; a test that `owners.yml` of both goldens loads with `load_owners` and that every `ALLOWED_KEYS`-based schema file equals `dump_schema()`; a test that every file below `src/knowledge_hub/governance/templates` matches a glob in `pyproject.toml`'s `package-data` (read with `tomllib`). A CLI test calls `main(["governance","render",...])` and checks the exit code and the files. Run once with `KH_UPDATE_GOLDEN=1` to create the golden files, read them, and commit them.
      Stop with `status=blocked` if the workflow cannot keep every PR-controlled value out of `run:` text.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_governance_schema.py tests/unit/test_governance_render.py -q passes"
      - "ls tests/golden/canonical-all/.forgejo/workflows/kh.yml tests/golden/canonical-hr/owners.yml succeeds"
      - "grep -c \"actions/checkout\" tests/golden/canonical-all/.forgejo/workflows/kh.yml prints 0"
      - "d=$(mktemp -d); .venv/bin/python -m knowledge_hub.cli governance render --repo canonical-hr --audience hr --out $d exits 0 and ls $d/owners.yml $d/schema/frontmatter.schema.json $d/.forgejo/CODEOWNERS $d/.forgejo/pull_request_template.md $d/.forgejo/workflows/kh.yml succeeds"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-validate-rules
    title: Pure validation rules, messages and the parser-agreement corpus
    depends_on: [p1-shared-core, p2-config-owners]
    complexity: M
    touches:
      - src/knowledge_hub/governance/messages.py
      - src/knowledge_hub/governance/rules.py
      - tests/unit/test_validate_rules.py
      - tests/data/validate/**
    brief: |
      Implement design 3.3 and 3.6. `TYPE_FOLDERS` is imported from `knowledge_hub.governance` (phase p2).
      1. `messages.py`: `class Code(enum.Enum)` whose members are exactly the ones named in 3.6, each value the text with `{}` placeholders as written there (`PARSE` is `"{}"`); `@dataclass(frozen=True) class Finding: path: str; code: Code; detail: str | None = None`; `render_line(f: Finding) -> str` returning `f"{f.path}: {f.code.value.format(f.detail) if '{}' in f.code.value else f.code.value}"`; `sort_findings(list) -> list` ordering by path then `code.name`. Add a module constant `MAX_FILE_BYTES = 1048576` and `MAX_FILES = 200`.
      2. `rules.py`: `@dataclass(frozen=True) class Layout: domain: str; product: str | None; component: str | None; kind: str | None; name: str; is_adr: bool; is_glossary: bool`; `parse_layout(path: str, domains: list[str]) -> Layout | None` implementing 3.3 exactly (reject a component named like a `TYPE_FOLDERS` key, an ADR without a component, a name that is empty, any path with a dot-prefixed segment returns `None`, so callers skip those before calling); `id_from_path(path, domains) -> str | None` (`glossary` for `glossary.md`; `adr-<component>-<NNNN>` for an ADR; the name for others); `expected_type(layout) -> str`. Constants `ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")`.
      3. `validate_files(files: Sequence[FileInput], *, config: Config, owners: OwnersFile, ids_in_repo: Mapping[str, Collection[str]], ids_elsewhere: Collection[str], entity_keys: Mapping[str, Collection[str]], repo_audience: str) -> list[Finding]` with `FileInput(path: str, text: str)`; follow the order and rules of 3.6 literally. Details: The entity regex is `re.compile(rf"({'|'.join(map(re.escape, config.entities))}):[a-z0-9.-]+")` full match. `ENTITY_NOT_ALLOWED` applies when `spec.reference_audiences is not None and repo_audience not in spec.reference_audiences`. `ids_in_repo` maps an id to the paths that hold it in the PR's head tree; a collision is a path other than the file's own. Product and component folder checks use `entity_keys.get("prd", ())` with the key `f"prd:{product}"` and `f"cmp:{component}"` (skipped when `config.entities` lacks `prd` or `cmp`). `SUPERSEDES` fires when a target is not a key of `ids_in_repo` or equals the file's own id. A glossary file needs `domain == "portfolio"`.
      4. Corpus: create `tests/data/validate/` with one `.md` file per case named `<case>.md` and a `cases.yaml` listing for each case the repo path it is validated at (`path`), the expected finding codes (`expect`, a list of `Code` names, empty for a pass), plus a shared `world` block (`ids_in_repo`, `ids_elsewhere`, `entity_keys`, `repo_audience`). Required cases (one file each): pass for a product-level requirement, a component-level spec, an ADR (`technical/gateway/api/adr/0001-use-grpc.md`, id `adr-api-0001`) and `glossary.md`; fail cases for every `Code` that `validate_files` can emit (layout, parse failure for `status: disputed`, unknown key, domain mismatch, bad id format, id not equal to filename, id in use in the repo, id in use elsewhere, missing type, wrong type, missing owner, wrong owner, bad entity format, `cus:` in an audience not in `reference_audiences`, `cus:` in an allowed audience that is missing from Grist, `prd:` unknown, unknown product folder, supersedes unknown target, supersedes itself). Keep each file under 15 lines.
      5. `tests/unit/test_validate_rules.py`: parametrised over `cases.yaml`: the sorted codes equal `expect`; for every case with an empty `expect`, `parse_document(text, path, config.domains)` returns a `ParsedDoc` (the agreement property: anything the gate passes, the indexer accepts); a test that every `Code` member except `TOO_LARGE`, `TOO_MANY` is exercised by at least one case; tests of `parse_layout` and `id_from_path` for the four path shapes, the ADR without component, a component named `adr`, a `.forgejo/` path; a test that `render_line` output for every finding contains only its own path and the fixed text (no other path from the world block).
      Stop with `status=blocked` if a passing corpus case is refused by `parse_document`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_validate_rules.py -q passes"
      - "ls tests/data/validate/cases.yaml succeeds"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-validate-command
    title: kh validate command, id index, Grist lookup, status post and no-leak test
    depends_on: [p3-schema-render, p4-forgejo-client, p5-validate-rules]
    complexity: M
    touches:
      - src/knowledge_hub/governance/ci.py
      - src/knowledge_hub/cli.py
      - tests/support/forgejo_http.py
      - tests/unit/test_cli_validate.py
    brief: |
      Implement design 3.5 and 3.7.
      1. `governance/ci.py`: `@dataclass class CheckResult: lines: list[str]; ok: bool`; `async def build_id_index(read: ForgejoClient, config: Config, own_repo: str, head: str) -> tuple[dict[str, list[str]], set[str]]` returning (id to paths in the own repo's head tree, ids of all other T0 repos), calling `head_sha_or_none` and `list_tree` with `asyncio.gather` over `config.repos` entries of tier `T0` (own repo: tree at `head`; a `None` head sha or a `ForgejoError` with `HTTP 404` skips that repo); `async def run_validate(read, status, grist, config, repo, base, head, *, post=True) -> CheckResult` that follows 3.7 step by step (read `owners.yml` with `get_raw(repo, "owners.yml", base)` and `load_owners`; `compare_files(repo, base, head)`; keep `.md` files that are not `removed` and have no dot-prefixed segment; `TOO_MANY` when more than `MAX_FILES`; read files with a `asyncio.Semaphore(8)`; `TOO_LARGE` for a text over `MAX_FILE_BYTES` encoded; build the index; collect the entity kinds cited by parsed frontmatter by running `parse_document` on each text once to read `entities` (a `Rejected` file cites none) and add `prd` and `cmp`; fetch those kinds' tables in parallel with `grist.fetch_table(config.entities[kind].table)` and build `entity_keys[kind]` from each record's `key` field; call `validate_files`; build lines with `render_line` and `ok = not findings`; post the status through `status.post_status(...)` when `post` is true, with the descriptions of 3.7, and post `error` then re-raise on an unexpected exception). Raise `CiError(RuntimeError)` for config problems (a missing `prd`/`cmp` kind in `config.entities` when a folder needs it, a missing `owners.yml`).
      2. `cli.py`: add parser `validate` with the flags of 3.7, dispatched right after `parser.parse_args(argv)` before the `serve` branch and the `args.command not in (...)` gate; `_validate(args)` builds `Settings`/`Config` (`load_config(args.config or settings.config_file)`), two `ForgejoClient`s (read token; status token when `--status-token-file` is set, else the same client), a `GristClient`, runs `run_validate`, prints the lines to stdout, returns 0 or 1; any `ForgejoError`, `GristError`, `CiError`, `OSError` or `ValueError` prints one line `validate: <message>` to stderr and returns 2; clients are closed in `finally`. Missing required values (`--forgejo-url`/`KH_FORGEJO_URL`, `--token-file`, Grist values) return 2 with `validate: <name> is required`.
      3. `tests/support/forgejo_http.py`: a small class `FakeForgejoHttp` wrapping `httpx.MockTransport` that serves, for a dict of repos (`{repo: {"sha": ..., "tree": {path: text}, "compare": [...]}}`), the endpoints `GET /api/v1/repos/<org>/<repo>/branches/<branch>`, `.../git/trees/<sha>`, `.../raw/<path>` (with the `ref` query), `.../compare/<base>...<head>`, records every request in `.requests` and every `POST .../statuses/<sha>` body in `.statuses`; plus a Grist fake `grist_transport(tables: dict[str, list[str]])` serving `/api/docs/<doc>/tables/<table>/records`.
      4. `tests/unit/test_cli_validate.py` (use `ForgejoClient(..., transport=...)` and call `run_validate` directly, and call `main(["validate", ...])` once with monkeypatched client factories for the exit-code test): a clean PR passes, posts `success` to `kh/validate`, exits 0; a PR with an unknown entity exits 1, posts `failure`; a Forgejo 500 exits 2 and posts `error`; `--no-status` posts nothing; the status goes to the status client when `--status-token-file` is given; request budget (a PR with 20 changed files in a world with 4 canonical repos makes at most 1 compare + 20 raw + 1 owners read + 8 tree/branch calls + the Grist tables, assert on `.requests`); the id index never calls `get_raw` for other repos. No-leak test: world where `canonical-hr` holds a document with the id the PR uses and a Grist `Customers` table holds `cus:acme`; the PR in `canonical-all` reuses the id and cites `cus:acme`; assert the output is exactly `<path>: id already in use` and `<path>: entity cus:acme is not allowed in this repository`, and that for the whole output text none of `canonical-hr`, the other repo's path, `acme corp` (the Grist customer's name column), nor the other repo's file name occurs; repeat with the colliding id in the same repo and assert the same text. Agreement test: every passing case of `tests/data/validate/cases.yaml` run through `run_validate` passes and `parse_document` accepts it.
      Add a test for a Grist record without a `key` field (it is ignored, so an entity cited from it is `ENTITY_NOT_FOUND`).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_cli_validate.py -q passes"
      - ".venv/bin/python -m knowledge_hub.cli validate --help exits 0 and lists --base and --head"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-domain-owners
    title: kh domain-owners command and its approval cases
    depends_on: [p2-config-owners, p4-forgejo-client, p6-validate-command]
    complexity: M
    touches:
      - src/knowledge_hub/governance/owners_check.py
      - src/knowledge_hub/governance/ci.py
      - src/knowledge_hub/cli.py
      - tests/support/forgejo_http.py
      - tests/unit/test_domain_owners.py
    brief: |
      Implement design 3.8 (kh domain-owners).
      1. `governance/owners_check.py`: `@dataclass(frozen=True) class OwnersProblem: pattern: str; team: str; text: str`; `def check_owners(*, owners: OwnersFile, paths: Sequence[str], counted: Sequence[Mapping[str, Any]], members: Mapping[str, Collection[str]], widen: tuple[str, Collection[str]] | None, widen_reason_ok: bool, widen_label_problem: str | None) -> list[str]`, pure: `counted` is the output of `current_approvals`; `members` maps a team name to the set of logins that are members (only for logins in `counted`); returns output lines `<pattern>: approval from <team> required` (pattern is the first entry of the matching rule's `paths` that `glob_to_regex` matches for some touched path), `<path>: no owner in owners.yml` for a path with no rule, `widen-audience needs approval from one of: <teams>` for an unsatisfied widen requirement, `widen-audience needs a Reason for widening line` and `widen-audience needs a destination label widen-audience/<audience>` when the given flags say so. Sorted, de-duplicated.
      2. `ci.py`: `async def run_domain_owners(read, status, config, repo, pr_number, *, post=True) -> CheckResult` as the steps of 3.8: `read.pull`; if `pr.get("state") != "open"` return `CheckResult(["pull request is not open"], True)` and post nothing; `owners.yml` at `pr["base"]["sha"]`; `compare_files(repo, pr.get("merge_base") or pr["base"]["sha"], pr["head"]["sha"])` and the paths from `filename` and `previous_filename`; `read.pull_reviews`; `counted = current_approvals(pr, reviews, BOT_LOGINS)`; teams to ask about = every team of a matching rule plus the widen teams; resolve each with `team_id` (an unknown team is `CiError`) and `is_team_member` for each counted reviewer login (cache in a dict); labels from `pr["labels"][i]["name"]`; widen label parsing as 3.8 (exactly one label starting `widen-audience/`, audience in `config.audiences`; the bare label `widen-audience` alone sets `widen_label_problem`); the reason is the first match of `(?mi)^Reason for widening:[ \t]*(\S.*)$` in `pr["body"]` whose text does not start with `<`; post the status on `pr["head"]["sha"]` with context `kh/domain-owners` and the descriptions of 3.7.
      3. `cli.py`: parser `domain-owners` with the flags of 3.8, dispatched like `validate` in p6, and the same settings fallbacks and exit codes as `validate` (message prefix `domain-owners: `).
      4. Extend `tests/support/forgejo_http.py` with the endpoints `GET .../pulls/<n>`, `.../pulls/<n>/reviews`, `GET /api/v1/orgs/<org>/teams`, `GET /api/v1/teams/<id>/members/<login>` (state given as dict team to members).
      5. `tests/unit/test_domain_owners.py` with a world built for `canonical-all` (owners from `build_owners("all", ...)` dumped with `dump_owners`; teams `owners-portfolio`, `owners-product`, `owners-technical`, `kh-admins`; users `alice` (PR author), `bob` (owners-product), `carol` (owners-technical), `dave` (kh-admins), `kh-hub-bot`). Cases: (a) approval from `bob` does not unlock a PR touching `technical/...`; with `carol` it does; (b) self-approval by the author does not count; approval by `kh-hub-bot` does not count even when it is a team member; (c) an approval whose `commit_id` is an older sha does not count (stale after a new push); a dismissed review and a non-official review do not count; (d) a PR touching `schema/frontmatter.schema.json` needs `kh-admins` (`bob` is not enough, `dave` is); (e) a PR touching both `product/` and `technical/` needs both teams; (f) a widen PR: label `widen-audience/hr`, reason line present: needs an approval from a member of `owners-hr` (add that team) besides the path owners; missing reason, a bare label and an unknown audience each fail with the lines of step 1; (g) a closed PR passes with no status posted; (h) the status is posted to the head sha with context `kh/domain-owners` and state `success` or `failure`; (i) a team missing from Forgejo exits 2; (j) a renamed file counts both paths. Also test `check_owners` directly for output text.
      Stop with `status=blocked` if `current_approvals` has to change to make a case pass; the approval definition is shared and fixed by phase p1.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_domain_owners.py tests/unit/test_cli_validate.py -q passes"
      - ".venv/bin/python -m knowledge_hub.cli domain-owners --help exits 0 and lists --pr"
      - "grep -n \"current_approvals\" src/knowledge_hub/governance/ci.py src/knowledge_hub/indexer/trust.py matches both files"
      - ".venv/bin/python -m ruff check . passes"
  - id: p10-retire-docs
    title: Governance docs, configuration reference, README and changelog
    depends_on: [p3-schema-render, p6-validate-command, p7-domain-owners, p9-privacyfence-docs]
    complexity: M
    touches:
      - docs/governance.md
      - docs/configuration.md
      - docs/design.md
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
    brief: |
      This is the retirement phase; it only writes documentation.
      1. Create `docs/governance.md` (under 250 lines): what `kh validate`, `kh domain-owners` and `kh governance render` do with their flags and exit codes; the layout and type table of plan 3.3; the table of differences D1-D16 with decisions copied from `docs/plans/10/plan.md` section 3.2; how the statuses get the exact context names (Forgejo derives `<workflow> / <job> (<event>)`, so the commands post their own; token scopes `read:repository` plus `read:organization` unrestricted for the read token and `write:repository` for the status token); the supported triggers per the research findings (`pull_request_target` and `workflow_dispatch` documented, `pull_request_review` not documented, off by default, `--review-trigger`), the fallback (an n8n workflow on the Forgejo review webhook calling the workflow dispatch API with `ref: main` and `inputs.pr`, with the exact request shape) and the live probe; the notes for the compose repo (labels `widen-audience/<audience>`, whitelist the destination owner teams, no seeded `README.md`, runner image built `FROM` the hub image, secrets and variables of the workflow); how to regenerate the golden files (`KH_UPDATE_GOLDEN=1`).
      2. `docs/configuration.md`: add rows for `entities.<kind>.reference_audiences` and `governance.owner_teams` (the setting `KH_FORGEJO_ORG` already has a row at `:45`; change its text to say the new commands use it too); add the three commands to the commands table (`:104` area); add a sentence that the runner needs `KH_CONFIG_FILE` in its environment when `kh.yaml` is not the default config.
      3. `docs/design.md`: add one bullet to the frontmatter rules (`:55` area) that `generated_by` and `derived_from` are also forbidden and that paths with a dot-prefixed segment are not indexed; link `governance.md`. `docs/runbook.md`: add a "Governance checks" paragraph with the three alert texts of the validator and where to look (link to `governance.md`), and add `generated_by` and `derived_from` to the forbidden key list in the alert table row at `:84`.
      4. `README.md`: add three rows to the table "The `kh` commands" (`:27`) for `kh validate`, `kh domain-owners` and `kh governance render`, each linking `docs/governance.md`.
      5. `CHANGELOG.md`: under `## [Unreleased]` add `### Added` bullets for the three commands and the governance templates and `### Changed` bullets for the two newly forbidden frontmatter keys, the dot-path skip and `email_for_visibility` in the PrivacyFence declaration example. No version heading.
      Do not edit `docs/plans/**`. Stop with `status=blocked` if a statement in `docs/governance.md` contradicts the code of a merged phase, and name the file.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - "grep -n \"Unreleased\" -A12 CHANGELOG.md shows Added and Changed entries and no heading with a version number between Unreleased and 0.1.1"
      - "ls docs/governance.md succeeds and wc -l docs/governance.md is below 250"
      - ".venv/bin/python -m pytest -q passes"
```
