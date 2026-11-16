# Companion: compose repo changes for agent writes

> Historical record once merged: read it only to understand why a change was made. It belongs to [plan 16](plan.md) of knowledge-hub and becomes its own plan in `andras-tkcs/tkcs-work-compose`. Nothing here is built yet.

## What the hub needs from the compose repo

Every new flag defaults to false, so converging with these changes creates and enables nothing.

| Area | Change (file in the compose repo) |
|---|---|
| New bot | `roles/forgejo_kh/tasks/bots.yml`: add `{name: kh-notes-bot, scopes: "write:repository", owner: "10001", token_file: kh_notes_bot_token}` to `kh_bots`, created only when `kh_notes_bot_enabled` is true. `group_vars/all/kh.yml`: `kh_notes_bot_enabled: false` and `kh_bot_emails["kh-notes-bot"]: "kh-notes-bot@tkcs.name"` (reserve the address as a Google Group like the other bots). Nobody may sign in as it: local account, random password, in no owner team. |
| Team | `roles/forgejo_kh/tasks/desired.yml`: new team `bots-notes` (`access: write`, `include_all: false`, repos every `notes-*` repo (`_notes`), members `[kh-notes-bot]`), created only when `kh_notes_bot_enabled`. `kh_units_write` already sets `repo.actions: none`. `bots-write` keeps canonical repos only and `kh-hub-bot` stays out of every notes repo. The bot is not on any approvals or merge whitelist and is not an org admin. Forgejo gives the team write on whole notes repos (any path, any branch); the restriction to `agent/<id>.md` on `main` exists only in the hub's client. Optional: protect `main` of the notes repos against force-push. |
| Secret | `kh_notes_bot_token` is written once by `forgejo_kh` into `{{ secrets_root }}/kh_notes_bot_token`, mode 0400, owner 10001. `roles/knowledge_hub/tasks/main.yml` (secret list) and `templates/compose.yaml.j2` (secrets and the lines `KH_FORGEJO_NOTES_TOKEN_FILE`, `KH_AGENT_NOTES_ENABLED`, `KH_AGENT_PROPOSALS_ENABLED`) add it, each behind its flag. |
| Flags | `group_vars/all/kh.yml`: `kh_notes_bot_enabled: false`, `kh_agent_notes_enabled: false`, `kh_agent_proposals_enabled: false`, `kh_agent_email_domains: ""` (the hub refuses to start with a write flag and no allowed email domain). The two agent flags need `knowledge_hub_enabled`; `kh_agent_notes_enabled` needs `kh_notes_bot_enabled` (guard task with a message naming the missing flag). |
| Branch prefix | Nothing to change in Forgejo: `kh-hub-bot` already pushes any branch in the canonical repos, and `main` stays protected. The hub's client now accepts `bot/propose/` besides `bot/promote/`. |
| Validator | The `kh domain-owners` change ships inside the hub image (the runner image is built `FROM` it): release the hub, then bump `images_track` and run `scripts/pin-images.sh`. No workflow change. `kh-ci` already reads commit statuses with `read:repository`. The token behind `KH_CI_STATUS_TOKEN` must belong to a dedicated account, never to `kh-hub-bot`, or the requester record could be forged (add a guard task and a test). |
| PrivacyFence | `roles/privacyfence/templates/upstream_mcp.yaml.j2`: add the five tools (`write_note` popup/update, `propose_doc` popup/add, `propose_edit` popup/update, `pr_status` and `list_my_proposals` auto/read) with the effect texts of plan 16 section 3.11. The template also lacks `identity_claim: required`, `email_for_visibility: true` and `allowed_principal_types: [claude_client, n8n_workflow]` that the hub's example declares; bring it in line in the same change. |
| Tests | `tests/fake-vars.yml`: the new variables and the bot email. `tests/forgejo-kh-desired.yml`: with the flag on, `bots-notes` has exactly the `notes-*` repos, members `== ['kh-notes-bot']`, and no canonical repo; `kh-notes-bot` is in no other team; `bots-write` repos contain no `notes-*`; with the flag off no `bots-notes` team and no `kh-notes-bot` entry. `tests/validate-upstream.sh`: the declaration lists the five tools. |
| Docs | `docs/knowledge-hub.md`: section 2 (bots and teams) and the acceptance list: "`kh-notes-bot` has write on `notes-*` only and cannot push to any canonical repo; the `agent/<id>.md` restriction is the hub's, not Forgejo's"; "`kh-hub-bot` still cannot write `notes-*`"; "a requester's own approval of an agent proposal leaves `kh/domain-owners` red". Token rotation: create the new token under `kh-notes-bot` with scope `write:repository`, replace the secret file, restart the hub, revoke the old token. |

## Order of rollout

1. Release the hub with the flags off; bump and pin the image; converge (nothing created).
2. `kh_notes_bot_enabled: true`; converge; check the user, the team and the secret file.
3. Run the live probes of plan 16 manual step ma1 (status creator, bot limits).
4. Enable `kh_agent_notes_enabled` and `kh_agent_proposals_enabled`; converge; restart PrivacyFence with the new declaration.
5. An administrator creates the scoped tokens and ticks the three write tools only for the people and agents that need them.

## Open points for the compose plan

- The `kh_team_members` rule that a Forgejo login is the lower-cased email local part is now a contract with the hub (it computes the requester login the same way). A person whose login differs is not excluded by name; keep the rule or tell the hub.
- Whether `kh-notes-bot` also needs `read:organization`; the hub reads through the indexer token, so the plan assumes no.
- `propose_change` PRs have the same requester gap; a follow-up in the hub can post the same status for them.
