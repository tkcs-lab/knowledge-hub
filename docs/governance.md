# Governance checks for canonical repositories

The hub ships the two required checks of every `kh/canonical-*` repository and the seed files that call them. The hub's own parser and trust rules are the source of truth, so CI is never looser than the indexer. Decisions behind this page: [plan 10](plans/10/plan.md).

## Commands

| Command | What it does | Exit |
|---|---|---|
| `kh validate --repo R --base SHA --head SHA` | Checks every `.md` file the pull request adds or changes (compare `base...head`, files read at `--head`, `owners.yml` read at `--base`). Prints one `path: reason` line per problem and posts the commit status `kh/validate` on `--head`. | 0 pass, 1 problems, 2 error |
| `kh domain-owners --repo R --pr N` | Checks that every owner team named by `owners.yml` for the changed paths has a counted approval. Prints one `<pattern>: approval from <team> required` line per gap and posts `kh/domain-owners` on the PR head. A PR that is not open prints `pull request is not open`, exits 0 and posts nothing. | 0 pass, 1 gaps, 2 error |
| `kh governance render --repo R --audience A --out DIR` | Writes the seed files of a repository's first commit (below). | 0 written, 2 error |

Flags. `validate`: `--forgejo-url`, `--forgejo-org`, `--token-file` (required), `--status-token-file`, `--grist-url`, `--grist-key-file`, `--grist-doc-id`, `--config`, `--no-status`. `domain-owners`: `--forgejo-url`, `--forgejo-org`, `--token-file` (required), `--status-token-file`, `--config`, `--no-status`. `governance render`: `--org` (`kh`), `--portfolio-team`, `--product-team`, `--technical-team`, `--owner-team` (default `owners-<audience>`), `--admin-team` (`kh-admins`), `--review-trigger`, `--runner-label` (default `kh-validate:host`; a name with an optional `:host`, and `runs-on` gets the name).

Missing flags fall back to `KH_FORGEJO_URL`, `KH_FORGEJO_ORG`, `KH_GRIST_URL`, `KH_GRIST_KEY_FILE`, `KH_GRIST_DOC_ID` and `KH_CONFIG_FILE` ([configuration](configuration.md)). Errors go to stderr with a fixed text, never a token or a response body. `--no-status` skips the status post. More than 200 changed `.md` files, or a file over 1 MiB, fail with a line (exit 1).

Messages come from a closed set of texts. No line names another repository, a path or a customer, so a failing check never reveals what exists elsewhere. A restricted entity kind fails as "not allowed in this repository" before any lookup.

### Widening an audience

A PR that moves content into a wider audience carries the label `widen-audience/<audience>` (the audience must be in `audiences`) and a body line `Reason for widening: <text>` with real text (text starting with `<` is the template placeholder and fails). `kh domain-owners` then also requires one counted approval from a member of an owner team of the destination audience (`governance.owner_teams`, else `owners-<audience>`; for `all` the three domain teams). A bare `widen-audience` label fails with `widen-audience needs a destination label widen-audience/<audience>`.

A counted approval is the trust rule's: `APPROVED`, official, not dismissed, on the current head commit, not by the author or `kh-hub-bot`. A reviewer in two teams counts for both.

## Layout and types

| Path | `type` | `id` |
|---|---|---|
| `glossary.md` | `glossary` (`domain: portfolio`) | `glossary` |
| `<domain>/<product>/<kind>/<name>.md` | by kind | `<name>` |
| `<domain>/<product>/<component>/<kind>/<name>.md` | by kind | `<name>` |
| `<domain>/<product>/<component>/adr/NNNN-<slug>.md` | `adr` | `adr-<component>-NNNN` |

`<domain>` is one of `domains` in `kh.yaml`. Kinds: `requirements` → `requirement`, `specs` → `spec`, `guides` → `guide`, `adr` → `adr`. `<product>` must exist as `prd:<product>` and `<component>` as `cmp:<component>` in Grist; a component folder is never a kind name. Ids and folders use lowercase letters, digits and single hyphens. `owner` equals the domain's team in `owners.yml`; keys outside the allowed set are rejected; `generated_by` and `derived_from` are forbidden. Paths with a dot-prefixed segment (`.forgejo/...`) are skipped by the gate and the indexer. Any other `.md` path fails with "path does not match the repository layout".

## Differences from the compose repo's rules (D1-D16)

The hub is the source of truth; the compose repo's `docs/knowledge-hub.md` section 4 differs as follows.

| # | Compose doc says | Decision |
|---|---|---|
| D1 | `title` not listed | Keep: `title` is required. |
| D2 | `status` may be `draft` or `disputed` | Hub keeps `current` and `superseded`. `draft` is rejected (an open PR is the draft); `disputed` is server-assigned. |
| D3 | `type` enum tied to the path | Gate only: `type` required and tied to the path. Parser unchanged. |
| D4 | `owner` is the folder's owner team | Gate only: `owner` must equal the domain's team in `owners.yml` at the base commit. |
| D5 | `generated_by`, `derived_from` forbidden | Parser change: both added to the forbidden keys (10 in total). |
| D6 | `additionalProperties: false` | Gate rejects unknown keys; the parser stays tolerant so an upgrade cannot drop live T0 documents. |
| D7 | `id` matches `^[a-z0-9-]+$` | Gate requires `^[a-z0-9]+(-[a-z0-9]+)*$`. |
| D8 | filename is `<id>.md`; ADR id `adr-<component>-NNNN` | Gate only. |
| D9 | `glossary.md` has `type: glossary`, `domain: portfolio`, owned by `kh-admins` | Gate only; `owners.yml` maps it to `kh-admins`. |
| D10 | a superseded target must be `status: superseded` | Rejected: the server assigns it. Targets must exist in the same repo and not be the file itself. |
| D11 | entity regex fixed to `prd\|cmp\|ver\|cus`; `cus:` only in contracts and mgmt | Config change: the regex follows `entities`; `reference_audiences` per kind says where it may be cited (`cus` default: contracts, mgmt). |
| D12 | mapping file `schema/owners.yml` | `owners.yml` at the repo root; `.forgejo/CODEOWNERS` is generated from it. |
| D13 | id unique across all `kh/*` repos | Indexer keeps its per-repo rule. The gate adds a best-effort rule over the repos its token can read; a repo it cannot read (404 or 403) is skipped without a word. Cross-audience uniqueness is not a CI check and the hub does not need it. |
| D14 | governance files live in the repo | Indexer skips dot-prefixed paths. A root `README.md` still raises an alert. |
| D15 | `pull_request_review` re-runs `kh/domain-owners` | Not documented in Forgejo v15; off by default (below). |
| D16 | status named `kh/validate` | A job's context is derived, so the commands post their own (below). |

## Status contexts, tokens and triggers

Forgejo names a job's status `<workflow> / <job> (<event>)`, so branch protection cannot require `kh/validate` from the job. The commands post `kh/validate` and `kh/domain-owners` themselves (`failure` with "<n> problems", `error` with "check could not run", `success` with "all checks passed"); the client accepts only those two contexts. The workflow's derived contexts (`kh / validate (pull_request_target)`) are extra; do not require them.

| Token | Scopes |
|---|---|
| Read (`KH_CI_TOKEN`) | `read:repository` and `read:organization` (a token limited to selected repositories cannot hold the organization scope); what it can read is limited by its CI user, who reads only its audience's repository |
| Status (`KH_CI_STATUS_TOKEN`, optional) | `write:repository`; give it to a dedicated account, since it can also push branches |

Without the status token the read token posts the status, which fails when it is read-only.

## One CI identity per audience

- Each audience has its own CI user (token), repo-level runner and secrets. The token reads only its audience's repo.
- `kh validate` never assumes it can read other repos: a repo it cannot see (403 or 404) is skipped, and any other error on another repo stops the run with a message that names no repo.
- The Grist key needs `Products` and `Components` always, and the table of a kind (default `Customers`) only in repos whose audience is in that kind's `reference_audiences`. Elsewhere any citation of that kind fails without a Grist read.
- `kh domain-owners` needs the CI user to list the organisation's teams and read team membership. This is an assumption until the live probe ma1 of the pull request that introduced this section confirms it.

Triggers (per the research of 2026-11-16, medium confidence): `pull_request_target` (`opened`, `synchronize`, `reopened`) and `workflow_dispatch` (input `pr`) are documented for Forgejo v15; `pull_request_target` runs the workflow from the base repository's default branch. `pull_request_review` is not documented, so `kh governance render --review-trigger` is needed to render it and it is off by default. Before turning it on, confirm with the live probe that a review event starts a run that reads the base branch's workflow.

Fallback for review events: an n8n workflow on the Forgejo review webhook calls the workflow dispatch API, and `kh domain-owners` (not n8n) computes the status, so the approval logic exists once:

```
POST {forgejo}/api/v1/repos/{org}/{repo}/actions/workflows/kh.yml/dispatches
Authorization: token <dispatch token>
Content-Type: application/json

{"ref": "main", "inputs": {"pr": "<pull request number>"}}
```

Live probe (manual): on a test PR confirm both statuses appear on the head commit, approve as a team member, run `kh.yml` by hand with `workflow_dispatch` and `pr`, and confirm `kh/domain-owners` turns green. Use a PR with more than 50 changed markdown files to confirm that compare lists every one.

## Seed files

`kh governance render` writes, deterministically, under `DIR`: `schema/frontmatter.schema.json` (JSON Schema 2020-12 from the parser's constants), `owners.yml`, `.forgejo/CODEOWNERS` (advisory; the CI check is authoritative), `.forgejo/pull_request_template.md` and `.forgejo/workflows/kh.yml`. Audience `all` uses the three domain teams; any other audience uses `--owner-team`. Rules in `owners.yml` are first match wins; `**` matches across `/`, `*` and `?` do not.

The workflow runs the base branch's definition, has no checkout step, passes PR fields through `env:` and never into `run:`, and writes tokens to a `umask 077` temporary file that it deletes on exit. Secrets: `KH_CI_TOKEN`, `KH_CI_GRIST_KEY`, optional `KH_CI_STATUS_TOKEN`. Variables: `KH_GRIST_URL`, `KH_GRIST_DOC_ID`, optional `KH_FORGEJO_URL` (default: the server URL).

## Notes for the compose repo

- Create the labels `widen-audience/<audience>` for every audience a repo can be widened to.
- Put the destination owner teams on the approvals whitelist of repos that can be widened from; otherwise their approval is not official and the check can never pass.
- Do not seed a `README.md` (`auto_init`): the indexer raises an alert for it and the gate fails any PR touching it.
- Build the runner image `FROM` the hub image so `kh` is on the path; register one runner per audience with the label `kh-validate-<audience>:host` and render with `--runner-label` of the same value; the runner needs `KH_CONFIG_FILE` in its environment when `kh.yaml` is not the default.
- Apply branch protection with the contexts `kh/validate` and `kh/domain-owners`.

## Golden files

The render output is compared with `tests/golden/`. After an intended template or schema change, regenerate with `KH_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/unit/test_governance_render.py -q` and review the diff.
