# Plan: gate the write tools, fix stale groups after a repo move, guard the search embedder

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An administrator moves a record or document with `kh move rec:<uuid> --from mgmt --to all --by admin@example.com`, or with the new "Move an item" card on the panel's Pipeline tab. It prints one JSON line and exits 0 (moved), 1 (refused), 2 (bad arguments or config) or 3 (a document's removal pull request could not be opened). Claude connectors lose `move_item` and are refused `ingest_record` (`this credential may not call this tool`); n8n hub keys keep `ingest_record`, and `kh key create --tools move_item` now says `key: unknown tool move_item`. After a repo is reassigned to another group, the next push or `kh rebuild --repo R` corrects unchanged documents, and `kh check` names that command. With the http embedder, a search sends its query out only when every group it covers may be embedded; otherwise it runs on full text and each hit's `similarity` is null.

**Scope:** `move_item` leaves the MCP interface (code moves to `admin/move.py`, new `kh move` and a panel op `item.move`). `ingest_record` becomes hub-key only. The default OAuth scopes stay `kh:read kh:write`; `kh:write` then covers `propose_doc` and `mark_record_outdated` only. The indexer re-reads stored documents whose group differs from the config. `search` gains a full-text-only mode. No migration; the OAuth flow is unchanged.

**Security:** A prompt-injected record can no longer move or write content into a wider group through a Claude session. Residual: `propose_doc` and `mark_record_outdated` stay reachable from Claude; a document move still opens a "Move in" pull request that target-group members can read before approval. `--by` records who asked and does not authenticate them: anyone with the container's secrets can pass any admin email.

**Review focus:** the cutover for n8n and OAuth flows that ingest today (5); the admin caller of the move core and the removed `claude_client` check (3.3, p4); the panel op that adds `--by` from the session and never from the request body (3.5, p5); the NULL query vector in the search SQL (3.2, p2); re-reading only same-tier rows on a group mismatch (3.1, p1).

## 1. Goal

Issue [#69](https://github.com/tkcs-lab/knowledge-hub/issues/69), three findings of a security review:

1. (High) Write tools can push restricted content into a wider group with no approval, and untrusted record text can trigger them in a Claude session: `move_item` moves a record at once, `ingest_record` writes into any held group, every connector gets `kh:write`.
2. (Medium) After a group's repo is reassigned to another group, unchanged documents keep their old group and stay readable by it; `kh rebuild` does not fix it.
3. (Medium, http embedder only) `search` embeds every query externally, even for groups that may not be embedded.

The owner decided on 2026-11-20 (board ask `a-9647d4f7515720c5`): moves become admin-only through the CLI and the panel; `ingest_record` becomes hub-key only; the default OAuth scopes stay; search embeds only when every covered group is allowed.

## 2. Current state

- `src/knowledge_hub/tools/move.py:233-247`: `move_item` is an MCP tool for `claude_client` callers who hold both groups; a record moves at once through `kh.move_record` (`migrations/0005_move_record.sql:29-45`), a document through two Forgejo pull requests. `mcp/server.py:35` registers it; `auth/bearer.py:13` lists it in `WRITE_TOOLS`.
- `src/knowledge_hub/tools/records.py:52`: `ingest_record` accepts any caller that holds the audience. OAuth callers get it through `kh:write` (`auth/bearer.py:46-51`); `oauth/provider.py:41` and `oauth/routes.py:28` default the scopes to `kh:read kh:write`.
- `src/knowledge_hub/indexer/sync_plan.py:90-92`: a stored file with the same blob sha is "unchanged" and is not upserted, so `kh.documents.audience` and `tier` are never updated. `kh rebuild` (`indexer/rebuild.py:39-49`) only deletes `kh.repo_state`. `kh check` already prints `document <path> in <repo> has audience <a>, config says <b>` (`rebuild.py:96-98`).
- `src/knowledge_hub/tools/search.py:88` calls `deps.embedder.embed([query])` with no `may_embed` test; `search/similar.py:37` and `indexer/sync_chunks.py:93` test it. `search/sql.py:84-93` needs a query vector in the vector list.
- The panel (`panel/ops.py`, `panel/runner.py`) runs allowlisted `kh` subprocesses; `Op.is_config_op` (`ops.py:42`) is true for every `cli` op not named `health.*`. The panel container already has the database, Forgejo bot and S3 secrets (`docs/deployment.md`, secrets table).
- Tests: `tests/integration/test_move_item_record.py`, `test_move_item_doc.py`, `test_ingest_record.py` (27 `ingest_record` calls, tombstone tests at 199-226), `tests/unit/test_hub_keys.py:63`, `tests/unit/test_bearer_middleware.py:129,197`, `tests/integration/test_hub_keys_db.py:161`.

## 3. Design

### 3.0 Decisions and what was rejected

1. **Moves are an admin operation outside MCP** (owner decision). Rejected: keep the MCP tool for admins only (an injected admin session could still trigger it); run the move as `kh_indexer` or the migrator with plain SQL (duplicates the lock, tombstone and link logic of `kh.move_record`, and needs new grants); an in-process panel op (the panel imports neither MCP nor admin modules).
2. **`ingest_record` is hub-key only** (owner decision): n8n decides which group a record goes to. Enforced twice: the OAuth tool allowlist leaves it out, and the tool refuses any caller whose `principal_type` is not `n8n_workflow` (covers the dev identity, whose `tools` is `None`). Rejected: a database check in the records INSERT policy (needs migration 0007; the tool check plus the allowlist is enough because only the app reaches the table).
3. **Default OAuth scopes stay `kh:read kh:write`** (owner decision). Whether Claude connectors request `kh:write` themselves could not be confirmed offline, so a read-only default could silently disable `propose_doc` for everyone.
4. **No alert on moves or ingests into `all`.** Moves now need an operator; n8n ingests into `all` are routine. The move still logs at INFO (`move_item sub=admin:<email> id= from= to=`).
5. **Search: full-text-only mode** when any covered group may not be embedded (owner decision `strict`). Rejected: a zero vector (cosine is NaN); two SQL shapes (more to test).
6. **Regroup on sync by re-reading, not by a SQL update.** Rejected: `UPDATE kh.documents SET audience` in `_write` (skips re-validation, and chunks moved into a group that may be embedded would never be embedded); deleting the repo's documents in `kh rebuild` (new `doc:` ids break `replaced_by` links and usage keys).

### 3.1 Regroup on sync (p1)

New pure function in `src/knowledge_hub/indexer/sync_plan.py`:

```python
def mark_regrouped(
    rows: Iterable[tuple[str, str, str, str, str]], audience: str, tier: str
) -> list[tuple[str, str, str]]:
    """rows: (path, blob_sha, doc_key, audience, tier) as stored. Returns (path, blob_sha, doc_key) where
    blob_sha is "" for a row whose tier equals ``tier`` but whose audience differs from ``audience``."""
```

An empty blob sha never equals a tree sha, so `changed_paths`, `changed_tech_paths` and `build_sync_plan` treat the file as changed: its text is fetched and re-validated, and the existing `ON CONFLICT ... DO UPDATE` writes the new `audience`. The trigger `owner_propagate_to_chunks` moves the chunk rows; `plan_chunks` keeps a stored chunk only when it has the same "has a vector" state, so a move into a group that may not be embedded drops the vectors and a move into one that may embeds them.

`indexer/sync.py`: `_load` also selects `audience` and `tier` and returns the raw 5-tuples; `sync_repo` calls `mark_regrouped(rows, audience, "T0")` (audience from `group_for_repo`) and `_sync_tech` calls `mark_regrouped(rows, TECH_AUDIENCE, "T0a")`.

Rows whose **tier** differs are not touched: a group repo that later becomes a tech repo would otherwise be upserted under `doc_key = path` next to the old row with the same path and break `UNIQUE (repo, path)` on every sync. Those rows stay for `kh check` (it reports the tier) and `kh setup apply --prune`; when the new tech plan does not list a stored path it deletes the row as for any vanished file, so the sync still succeeds. A re-read file that now fails validation (a removed label, bad frontmatter) is deleted with an alert, as for any changed file.

`indexer/rebuild.py`: the audience line of `check` ends with `; run kh rebuild --repo <repo>` when the config names an active group for the repo (the `no group` variant and the archived-group variant are unchanged, because `kh rebuild --repo` refuses those repos).

### 3.2 Search embedder guard (p2)

`tools/search.py` computes, before embedding:

```python
covered = ({audience} & call.caller.audiences) if audience else call.caller.audiences
embed = bool(covered) and all(deps.embedder.may_embed(g) for g in covered)
vector = (await deps.embedder.embed([query]))[0] if embed else None
```

`search/sql.py`: `SearchFilters.query_vector` becomes `list[float] | None` (still required, no default). `build_search_query` sends `"qvec": None` for `None`. Both vector CTEs (`_lists`, the `main` and `low` lists) get `AND %(qvec)s::vector IS NOT NULL` in the `WHERE` of the inner select, so a NULL vector gives an empty vector list instead of `pool` arbitrary rows (with the similarity floor off, `max_dist` is 2 and `dist` NULL would otherwise pass). The `similarity` column is already NULL when `qvec` is NULL. The cursor is unchanged: the mode depends only on the caller's groups and `audience` (both already in the cursor HMAC) and on server configuration, like the similarity floor. A caller with no groups, or an `audience` the caller does not hold, never embeds.

### 3.3 Admin move (p4)

- `git mv src/knowledge_hub/tools/move.py src/knowledge_hub/admin/move.py`. Remove `register`, `DESCRIPTION`, the `MCPServer` and `tool_guard` imports and the `claude_client` check (`NOT_FOR_WORKFLOWS` is deleted from `mcp/errors.py`). `async def move_item(call: ToolCall, id: str, from_group: str, to_group: str) -> dict[str, Any]` keeps its body, including the `is_active` and `caller.audiences` checks (they pass because the admin caller below holds both groups). `tests/unit/test_import_boundary.py` bans `knowledge_hub.admin.*` from the serving app, which keeps the code from being registered as a tool again.
- New `src/knowledge_hub/admin/move_cmd.py`:
  - `async def do_move(deps: Deps, email: str, id: str, from_group: str, to_group: str) -> dict[str, Any]` builds `Caller(sub=f"admin:{email}", principal_type="admin", email=email, audiences=frozenset({from_group, to_group}), token_id=None, tools=None)` and runs `move_item` inside `caller_tx(deps.app_pool, caller)` with `ToolCall(conn, caller, deps, [])`. An administrator may move between groups they do not hold. `kh.caller_type()` is checked only by the usage-events policy, so no migration is needed.
  - `register(sub)` adds the parser `move` with positional `id`, required `--from` (dest `from_group`), `--to` (dest `to_group`), required `--by` and `--config` (default `KH_CONFIG_FILE`, through `add_common(parser, mutating=False)`, which also adds an unused `--json`).
  - The run function: lower-case `--by`; `build_deps`, `close_deps`, `load_settings`, `load_config` and `do_move` are imported at module level of `move_cmd.py` (so tests can patch them); exit 2 with `move: --config or KH_CONFIG_FILE is required` when `args.config` is `None` (`load_config(None)` returns an empty config); load the config; exit 1 with `move: <email> is not an administrator` when it is not in `config.admins`; refuse a group name that does not match `GROUP_RE` with exit 2 and `move: group name is not valid`; build the deps with `build_deps` and close them with `close_deps`; call `do_move`; print the result as `json.dumps(result, sort_keys=True)`; exit 3 when `result.get("note")` is non-empty; catch `KhError` and print `move: <exc.text>` with exit 1; a `ValueError` or `ConfigError` prints its text with exit 2. The output holds ids, group names, pull-request URLs and doc keys, never a body.
- `admin/cli.py`: `COMMAND_MODULES` gets `"move_cmd"`. `cli.py`: `move` joins `label`, `group`, `person`, `repo` in the dispatch to `admin_main`.
- `mcp/server.py` no longer imports or registers `move`. `auth/bearer.py`: `WRITE_TOOLS = {"mark_record_outdated", "propose_doc"}` (existing OAuth tokens lose `move_item` at their next call, because tools are computed per request). `auth/keys.py`: the comment above `KEY_TOOLS` says it equals the registered tools. `auth/keys_cli.py`: the special `move_item` branch is deleted, so `--tools move_item` gives `key: unknown tool move_item`. `mcp/errors.py`: `MOVE_PARTIAL` reads `the removal pull request could not be opened; run kh move again to open it`.
- The marker `Moved-from: doc:<id>` of the pull requests is unchanged, so moves opened by the old tool still resume. The Prometheus series `kh_tool_calls_total{tool="move_item"}` stops.

### 3.4 Ingest key-only (p3)

- `mcp/errors.py`: `ONLY_WORKFLOWS = "this tool is available to workflows only"`.
- `auth/bearer.py`: new `KEY_ONLY_TOOLS = frozenset({"ingest_record"})`; `WRITE_TOOLS` loses `ingest_record`; `__all__` lists it.
- `tools/records.py`: first statement of `ingest_record` is `if call.caller.principal_type != "n8n_workflow": raise kh_error("only_workflows", errors.ONLY_WORKFLOWS)`. The tool description says it is for hub keys only (the tool list is not filtered per caller).
- Tests use workflow callers: `tests/support/principals.py` gets `wf_all`, `wf_mgmt`, `wf_hr` (principal_type `n8n_workflow`, `sub` `key:<name>`, one group each) and `wf_none` (the same with no group).

### 3.5 Panel op `item.move` (p5)

- `panel/ops.py`: `Op` gets two fields, `config_op: bool = True` and `needs_by: bool = False`; `is_config_op` becomes `self.kind == "cli" and self.config_op`; the two `health.*` ops and `item.move` set `config_op=False`. New value rules: `"id"` (pattern `^(doc|rec):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`, error `item id is not valid`), `"from_group"` and `"to_group"` (the existing group-name rule). Op `item.move`: kind `cli`, keys `("id", "from_group", "to_group")`, `needs_by=True`, built as `["move", CFG, f"--from={…}", f"--to={…}", BY, "--", id]` where `BY = "\0by"` is a placeholder like `CFG`.
- `ops.argv(valid, config_file, by=None)`: replaces `BY` with `--by=<by>`; an op with `needs_by` and `by is None` raises `OpError("signed-in email is missing")`. A request that carries `by` in `args` is refused by the existing `unexpected argument` check.
- `panel/runner.py`: `_run` passes the `email` it already holds to `_run_cli(valid, secrets, email)`, which calls `ops.argv(valid, self.config_file, email)`. `restart_expected` stays false (the op is not a config op). `dry_run` and `config_sha256` are refused for it by the existing checks.
- `panel/static/pipeline.js`: a "Move an item" form (three text fields `id`, `from_group`, `to_group`, previewless, a fixed `confirmText`), shown with the other Pipeline actions; the job output shows the JSON line.

### 3.6 Facts a worker needs

- `Caller` is `knowledge_hub.auth.claims.Caller(sub, principal_type, email, audiences, token_id, tools=None)`. `KhError` has `.code` and `.text`; `kh_error(code, template, **fields)`.
- `parse_id` is `knowledge_hub.tools.docs.parse_id`.
- `GroupConfig` has an optional `repo` (default `kb-<name>`); `KhConfig.group_for_repo(repo)` returns the group of a repo.
- The integration tests need PostgreSQL: run `bash scripts/ensure_test_pg.sh` first.

## 4. Manual steps

### Before implementation

None.

### After implementation

**ma1-live-move (about 20 minutes).** What CI cannot check: a move against the real Forgejo and database. Run it in a staging deployment of the feature branch image (`docker build -t kh:69 .` from a checkout of the branch, same image tag for the hub, the directory sync and the panel, then `kh migrate`), or on the first release after the merge; say which in your report.

1. Create a hub key for `mgmt` (`kh key create --group mgmt --name movecheck`, see [the identity runbook](https://github.com/tkcs-lab/knowledge-hub/blob/fix/gate-write-tools-and-regroup/docs/runbook-identity.md)) and use it in a throw-away n8n workflow (or any MCP client) to call `ingest_record` once with audience `mgmt`; you see an id `rec:<uuid>`.
2. Run `kh move rec:<id> --from mgmt --to all --by <your admin email>` in the hub container. You see one JSON line with `"moved": true`.
3. Open the panel, Pipeline tab, "Move an item", and move a test document the same way. You see two pull request URLs in the job output; merge the add request first, then the removal request.
4. Report back: pass or fail for the record move, the document move and the panel card.

**ma2-n8n-and-connectors (about 10 minutes).** Run it against the same staging deployment, or the production deployment after the release.

1. In n8n, open every workflow that calls `ingest_record` and check that its credential is a hub key (`kh key list` shows the key and its group).
2. In a Claude connector session, ask for `ingest_record`; you see `this credential may not call this tool`.
3. Report back: the list of workflows checked and a pass or fail line.

## 5. Risks and open questions

- **Search recall with the http embedder.** Anyone holding one group that may not be embedded searches by full text on every unfiltered search; most people hold `all` plus a team, so this can cover most users. Early sign: `similarity` null on most hits. A counter for full-text-only searches is a follow-up. A worker that cannot make the SQL return only full-text hits with the floor at 0 stops with `status=blocked`.
- **n8n or agent flows that wrote through OAuth break after p3.** Early sign: `this credential may not call this tool` in logs and `kh_tool_calls_total{tool="ingest_record",outcome="error"}` rising.
- **One-off load after a repo reassignment.** The regrouped repo's documents are fetched and re-embedded once. Early sign: a sync that fails on a unique violation (a worker that sees one stops with `status=blocked`: the tier guard of 3.1 is wrong).
- **Panel timeout.** The panel stops an op after 300 s; a document move makes several Forgejo calls. Early sign: a `timed out` job; the CLI is the fallback.
- **`--by` is attribution only** (anyone with the container's secrets can pass any admin email); the design doc says so.
- **A "Move in" pull request still carries the full text** in the target repo, readable by its members before approval.
- **`propose_doc` and `mark_record_outdated` stay under `kh:write`.** A prompt-injected session can still open a pull request into a group it holds (an approver merges it) or hide records. The design doc's accepted-risk list is rewritten accordingly.
- **Tier drift stays manual.** A repo that changes between group repo and tech repo keeps the old rows until `kh setup apply --prune`.
- **Stop conditions in the briefs:** a worker that cannot reach PostgreSQL for the integration tests, or whose grep shows another caller of `move_item` or `NOT_FOR_WORKFLOWS` than the ones named, stops with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: gate-write-tools-and-regroup
feature_branch: fix/gate-write-tools-and-regroup
tracking_issue: 69
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-live-move
    title: Move a record and a document in the real deployment
    why: The Forgejo pull requests and the panel subprocess cannot be exercised by CI.
  - id: ma2-n8n-and-connectors
    title: Check n8n workflows use hub keys and connectors lose ingest_record
    why: Only the live n8n instance and a real connector show whether any flow still ingests through OAuth.
verify_after_merge:
  - .venv/bin/python -m pytest -q -k regroup
  - .venv/bin/python -m pytest -q -k embed_guard
  - .venv/bin/python -m pytest -q -k admin_move
  - .venv/bin/python -m pytest -q -k ingest
final_checks:
  - "`tld plan deviations check --file docs/plans/69/deviations.md --plan docs/plans/69/plan.md --all` exits 0"
  - "the changelog has entries under `## [Unreleased]` and no version heading"
phases:
  - id: p1-regroup-sync
    title: Re-read stored documents whose group differs from the config
    depends_on: []
    complexity: S
    touches:
      - src/knowledge_hub/indexer/sync_plan.py
      - src/knowledge_hub/indexer/sync.py
      - src/knowledge_hub/indexer/rebuild.py
      - tests/unit/test_sync_plan.py
      - tests/integration/test_sync_regroup.py
      - tests/integration/test_rebuild.py
    brief: |
      Design: section 3.1. Do not edit docs or CHANGELOG.md (p6 does). Run `bash scripts/ensure_test_pg.sh` before the integration tests.
      1. In `src/knowledge_hub/indexer/sync_plan.py` add `mark_regrouped(rows, audience, tier)` exactly as 3.1 specifies: for each row `(path, blob, key, row_audience, row_tier)` return `(path, "" if row_tier == tier and row_audience != audience else blob, key)`, in input order.
      2. In `src/knowledge_hub/indexer/sync.py` change `_load` so the `kh.documents` query selects `path, blob_sha, doc_key, audience, tier` and `_load` returns the 5-tuples as its second value. In `sync_repo` replace `stored` with `mark_regrouped(rows, audience, "T0")`; in `_sync_tech` with `mark_regrouped(rows, TECH_AUDIENCE, "T0a")` (import `TECH_AUDIENCE` from `knowledge_hub.config`). Use the marked list everywhere `stored` was used, including `changed_paths` / `changed_tech_paths`. `grep -rn "await _load(" src/knowledge_hub/indexer tests` must show only the two calls in `sync.py`; if it shows another, stop with `status=blocked`.
      3. In `src/knowledge_hub/indexer/rebuild.py` `check`, append `; run kh rebuild --repo {repo}` to the audience line only when `wanted` is not `None`. Update the two expected strings at `tests/integration/test_rebuild.py:114` and `:236` to end with `; run kh rebuild --repo kb-all`; the strings at `:250-251` and `:260-261` stay as they are.
      4. Unit tests in `tests/unit/test_sync_plan.py`: a row with the wanted tier and another audience gets `""`; a row with the wanted audience keeps its blob; a row with another tier keeps its blob; order is kept.
      5. New `tests/integration/test_sync_regroup.py`, modelled on `tests/integration/test_sync.py` (fixtures `world`, `fake`, `reader`, `sync`): (a) with a config where group `all` has `repo="kb-shared"`, sync `kb-shared` with one file; then with a config where group `mgmt` has `repo="kb-shared"` and `all` has `repo="kb-all"`, sync again at the same head: the row's `audience` is `mgmt`, its chunks' `audience` is `mgmt`, and `blob_sha`, `id` are unchanged; (b) the same with `FakeEmbedder` replaced by a stub whose `may_embed` is false for `mgmt`: after the second sync the chunks have `embedding IS NULL`; (c) after the regroup of (a), a file whose frontmatter names the label `ember` is regrouped and then re-validated: set it up by syncing the file with label `ember` first, then with admin SQL `UPDATE kh.documents SET labels = '{}'` and `DELETE FROM kh.labels WHERE name = 'ember'` (the delete is refused while a document uses the label), then regroup: the row is deleted and an alert `file rejected: unknown label ember` exists; (d) a stored row with tier `T0` at path `docs/a.md` in a repo that is now a tech repo (config with `tech_org` and `tech_repos` listing it with `dirs: [docs]`) is not re-read by the regroup and the sync does not raise (the tech plan deletes the vanished row). `tests/integration/test_sync.py` has the helper `sync` with its `CFG` hardcoded: write a local helper `sync_with(pool, reader, config, repo)` in the new file instead of importing it.
      Stop condition: if (a) shows the chunk `audience` unchanged, stop with `status=blocked` (the trigger `owner_propagate_to_chunks` is not firing).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_sync_plan.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_sync_regroup.py tests/integration/test_rebuild.py tests/integration/test_sync.py tests/integration/test_sync_tech.py -q passes"
      - "grep -n 'def mark_regrouped' src/knowledge_hub/indexer/sync_plan.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-search-embed-guard
    title: Embed a search query only when every covered group may be embedded
    depends_on: []
    complexity: S
    touches:
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/search/sql.py
      - tests/unit/test_search_sql.py
      - tests/integration/test_search_embed_guard.py
    brief: |
      Design: section 3.2. Do not edit docs or CHANGELOG.md (p6 does). Run `bash scripts/ensure_test_pg.sh` before the integration tests.
      1. In `src/knowledge_hub/search/sql.py` change `SearchFilters.query_vector` to `list[float] | None` (still without a default). In `build_search_query` set `"qvec"` to `None` when it is `None`, otherwise the existing bracketed string. In `_lists`, add `AND %(qvec)s::vector IS NOT NULL` to the `WHERE` of the inner select of `{prefix}_vec` (next to `c.embedding IS NOT NULL`).
      2. In `src/knowledge_hub/tools/search.py` replace line 88 with the three statements of 3.2 (`covered`, `embed`, `vector`) and pass `query_vector=vector`.
      3. Unit tests in `tests/unit/test_search_sql.py`: `build_search_query` with `query_vector=None` returns `params["qvec"] is None`; the SQL text contains `%(qvec)s::vector IS NOT NULL` exactly twice (once per vector list; other `IS NOT NULL` fragments of the statement are not counted); with a vector, `params["qvec"]` starts with `[`.
      4. New `tests/integration/test_search_embed_guard.py`, modelled on `tests/integration/test_search_floor.py` (fixtures `deps`, `db`, helper `hits`; seed with `add_chunked_doc` and `add_chunked_record`). Use a stub embedder (`model = "fake"`, `dim = 1024`, `may_embed(a)` true only for `"all"`, `embed` records its calls and delegates to `FakeEmbedder`). Cases, with the default `KH_SEARCH_MIN_SIMILARITY` of 0: (a) caller `multi` (groups `all`, `mgmt`), no `audience`: the stub's `embed` is not called, only items that match the query words by full text are returned, every hit has `similarity` null, and an item with no matching word is not returned; (b) the same caller with `audience="all"`: `embed` is called once and hits carry a non-null `similarity`; (c) caller `alice_all` (group `all`): `embed` is called; (d) caller `dave_none` (no groups): `embed` is not called and the result is empty; (e) caller `multi` with `audience="hr"` (not held): `embed` is not called.
      Stop condition: if (a) returns an item that matches no query word, stop with `status=blocked` (the vector list is not switched off).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_search_sql.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_search_embed_guard.py tests/integration/test_search_floor.py tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py -q passes"
      - "grep -n 'embed = ' src/knowledge_hub/tools/search.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3-ingest-key-only
    title: Make ingest_record hub-key only
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/auth/bearer.py
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/mcp/errors.py
      - tests/support/principals.py
      - tests/integration/test_ingest_record.py
      - tests/integration/test_archive_tools.py
      - tests/integration/test_similar_items.py
      - tests/unit/test_bearer_middleware.py
    brief: |
      Design: section 3.4. Do not edit docs or CHANGELOG.md (p6 does). Run `bash scripts/ensure_test_pg.sh` before the integration tests. `move_item` stays registered in this phase (p4 removes it).
      1. `src/knowledge_hub/mcp/errors.py`: add `ONLY_WORKFLOWS = "this tool is available to workflows only"` next to `NOT_FOR_WORKFLOWS`.
      2. `src/knowledge_hub/auth/bearer.py`: remove `"ingest_record"` from `WRITE_TOOLS`, add `KEY_ONLY_TOOLS = frozenset({"ingest_record"})` and put it in `__all__`.
      3. `src/knowledge_hub/tools/records.py`: first statement of `ingest_record` is the check of 3.4. Change the tool description to start `Store a raw record ... readable by the named audience (hub keys only; the audience must be the key's group).` keeping the rest of the existing sentences.
      4. `tests/support/principals.py`: add `wf_all`, `wf_mgmt`, `wf_hr` as in 3.4.
      5. Update the three integration files: every `mcp_client.call(<Claude caller>, "ingest_record", ...)` uses the workflow caller of the audience it writes to (`wf_all` for `all`, `wf_mgmt` for `mgmt`, `wf_hr` for `hr`); in `test_audience_not_held` (`test_ingest_record.py:98-104`) the `dave_none` call becomes a `wf_none` call that still expects `AUDIENCE_NOT_HELD`, and the `alice_all` call with audience `hr` becomes `wf_all` with audience `hr`; assertions about `created_by_sub` / `created_by_type` follow the workflow caller (`n8n_workflow`). The two tombstone tests at `test_ingest_record.py:199-226` keep calling `move_item` as the Claude caller for the move and re-ingest with the workflow caller. Add `test_ingest_refused_for_a_claude_client` (an OAuth-style caller `alice_all` with `tools=None` gets `ONLY_WORKFLOWS`) to `test_ingest_record.py`.
      6. `tests/unit/test_bearer_middleware.py`: line 129 (the caller's tools) stays `READ_TOOLS | WRITE_TOOLS`; at line 197 (the comparison with the registered tool names) use `READ_TOOLS | WRITE_TOOLS | KEY_ONLY_TOOLS`; in `test_scopes_limit_tools` (line 172-176) replace the case `(["kh:write"], "ingest_record", "search")` with `(["kh:write"], "propose_doc", "search")`; and add a test that an OAuth token with `kh:write` has `ingest_record` absent from `caller.tools`.
      Stop condition: if `grep -rn "ingest_record" tests` shows a call by a Claude caller in a file not listed in `touches`, stop with `status=blocked`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_bearer_middleware.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_ingest_record.py tests/integration/test_archive_tools.py tests/integration/test_similar_items.py -q passes"
      - "grep -n 'KEY_ONLY_TOOLS' src/knowledge_hub/auth/bearer.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-admin-move
    title: Move move_item out of MCP into kh move
    depends_on: [p3-ingest-key-only]
    complexity: M
    touches:
      - src/knowledge_hub/tools/move.py
      - src/knowledge_hub/admin/move.py
      - src/knowledge_hub/admin/move_cmd.py
      - src/knowledge_hub/admin/cli.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/auth/bearer.py
      - src/knowledge_hub/auth/keys.py
      - src/knowledge_hub/auth/keys_cli.py
      - tests/integration/test_move_item_record.py
      - tests/integration/test_move_item_doc.py
      - tests/integration/test_admin_move_record.py
      - tests/integration/test_admin_move_doc.py
      - tests/integration/test_ingest_record.py
      - tests/integration/test_hub_keys_db.py
      - tests/unit/test_hub_keys.py
      - tests/unit/test_bearer_middleware.py
      - tests/unit/test_admin_move_cmd.py
    brief: |
      Design: section 3.3. Do not edit docs or CHANGELOG.md (p6 does). Run `bash scripts/ensure_test_pg.sh` before the integration tests.
      1. `git mv src/knowledge_hub/tools/move.py src/knowledge_hub/admin/move.py`; `git mv tests/integration/test_move_item_record.py tests/integration/test_admin_move_record.py`; `git mv tests/integration/test_move_item_doc.py tests/integration/test_admin_move_doc.py`.
      2. In `admin/move.py` apply the edits of 3.3 (remove `register`, `DESCRIPTION`, the `MCPServer` and `tool_guard` imports and the `claude_client` check). Update the module docstring to `Move a record or a document between groups: the core of kh move (plan 69).`
      3. `mcp/errors.py`: delete `NOT_FOR_WORKFLOWS` and set `MOVE_PARTIAL` to the text of 3.3. `grep -rn "NOT_FOR_WORKFLOWS\|knowledge_hub.tools.move\|tools import .*move" src tests` must then show nothing; fix what it shows.
      4. `mcp/server.py`: drop `move` from the import and from the registration calls. `auth/bearer.py`: `WRITE_TOOLS = frozenset({"mark_record_outdated", "propose_doc"})`. `auth/keys.py`: the comment above `KEY_TOOLS` becomes `# The tools registered by build_mcp (a unit test compares this with build_mcp).` `auth/keys_cli.py`: delete the `if tool == "move_item"` branch.
      5. New `admin/move_cmd.py` as in 3.3 (`do_move`, `register`, the run function with the exit codes 0, 1, 2, 3). Register it: `COMMAND_MODULES` in `admin/cli.py` gets `"move_cmd"`; in `cli.py` add `"move"` to the tuple `("label", "group", "person", "repo")`.
      6. Port `test_admin_move_record.py` and `test_admin_move_doc.py` one for one: every `mcp_client.call(<caller>, "move_item", {...})` becomes `await do_move(deps, "admin@example.test", id, from_group, to_group)` with `KhError` caught where the old test asserted `res.is_error` (compare `exc.text` with the old expected text); these tests change because the admin caller holds both groups and has principal type `admin`: `test_move_needs_both_groups` (`test_move_item_record.py:77-82`) becomes `test_move_to_an_inactive_group_is_refused` (move to a group name that is not in the config: `AUDIENCE_NOT_HELD`); `test_workflow_principal_is_refused` (`:127-133`) is deleted; the log assertions (`test_move_item_record.py:142`, `test_move_item_doc.py:336`) expect `move_item sub=admin:admin@example.test id=... from=all to=mgmt`; every assertion about the pull-request trailer `Principal: claude_client` in `test_admin_move_doc.py` expects `Principal: admin` and `Requested-by: admin:admin@example.test`; add `test_admin_may_move_between_groups_they_do_not_hold` (the email `admin@example.test` is in no group and moves a record `mgmt` to `all`). The fixtures `deps` and `db` stay; the doc test's Forgejo fake stays.
      7. `tests/integration/test_ingest_record.py` tombstone tests: replace the `move_item` call with `do_move`.
      8. `tests/integration/test_hub_keys_db.py:161`: expected text becomes `key: unknown tool move_item`. `tests/unit/test_hub_keys.py:63`: `assert set(KEY_TOOLS) == names`. `tests/unit/test_bearer_middleware.py`: add `assert "move_item" not in READ_TOOLS | WRITE_TOOLS | KEY_ONLY_TOOLS`.
      9. New `tests/unit/test_admin_move_cmd.py` (no database; patch `build_deps`, `close_deps` and `do_move` with `monkeypatch`): an email not in `admins` gives exit 1 and `move: <email> is not an administrator`; a group name `A_b` gives exit 2; a `KhError` gives exit 1 with `move: <text>`; a result with a non-empty `note` gives exit 3; a normal result prints one JSON line and exits 0; `--by` is lower-cased.
      10. `.venv/bin/python -m pytest tests/unit/test_import_boundary.py -q` must pass.
      Stop condition: if `grep -rn "move_item" src` still shows a use outside `admin/move.py`, `admin/move_cmd.py` and the log line, stop with `status=blocked`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_move_cmd.py tests/unit/test_hub_keys.py tests/unit/test_bearer_middleware.py tests/unit/test_import_boundary.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_admin_move_record.py tests/integration/test_admin_move_doc.py tests/integration/test_ingest_record.py tests/integration/test_hub_keys_db.py -q passes"
      - "test ! -e src/knowledge_hub/tools/move.py"
      - "grep -rn 'NOT_FOR_WORKFLOWS' src tests returns no match"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-panel-move
    title: Add the item.move panel operation and its form
    depends_on: [p4-admin-move]
    complexity: M
    touches:
      - src/knowledge_hub/panel/ops.py
      - src/knowledge_hub/panel/runner.py
      - src/knowledge_hub/panel/static/pipeline.js
      - tests/unit/test_panel_ops.py
      - tests/unit/test_panel_runner.py
      - tests/unit/test_panel_api_ops.py
      - tests/unit/test_panel_static.py
    brief: |
      Design: section 3.5. Do not edit docs or CHANGELOG.md (p6 does). `kh move` exists after p4; its parser takes `--config`, `--from`, `--to`, `--by` and a positional id.
      1. `src/knowledge_hub/panel/ops.py`: add the `Op` fields and the `is_config_op` change of 3.5; pass `config_op=False` for `health.setup_plan` and `health.index_check` through `_cli(...)` (add a `config_op` parameter to `_cli`). Add `BY = "\0by"`, the three value rules in `RULES` and the op `item.move` exactly as 3.5. Extend `argv(valid, config_file, by=None)` as 3.5 says; the result for `item.move` with by `a@b.test` is `["move", "--config=<file>", "--from=<g>", "--to=<h>", "--by=a@b.test", "--", "<id>"]`.
      2. `src/knowledge_hub/panel/runner.py`: `_run` passes `email` to `_run_cli`; `_run_cli(self, valid, secrets, email)` calls `ops.argv(valid, self.config_file, email)`.
      3. `src/knowledge_hub/panel/static/pipeline.js`: add `moveCard(reload)` that returns `opForm({op: "item.move", fields: [{name: "id", label: "Item id (rec:... or doc:...)"}, {name: "from_group", label: "From group"}, {name: "to_group", label: "To group"}], previewless: true, submitLabel: "Move", confirmText: "Move this item? A record moves at once; a document opens two pull requests.", onApplied: (job) => { setNotice("Move", job); reload(); }, onBusy: <same handler as action()>}).form` and append it to the toolbar next to the existing `action(...)` buttons, under a heading `Move an item`.
      4. Tests: `test_panel_ops.py`: add an `item.move` entry to `EXAMPLES`; change `test_every_config_op_is_round_tripped` and the filter of `test_argv_round_trips_through_the_real_parser` (`not e[0].startswith("health.")`) so both also exclude names starting with `item.` (the `move` parser has no `--dry-run`, `--if-sha256` or `--no-apply`); update the operation-count test (`grep -n "operations" tests/unit/test_panel_ops.py`) by one; add a round-trip test of `item.move` against `admin.cli`'s real parser that checks `ns.id`, `ns.from_group`, `ns.to_group`, `ns.by` and `str(ns.config)`; add tests that `args` containing `by` is refused (`unexpected argument: by`), that `dry_run=True` is refused for `item.move`, that `config_sha256` is refused for it, that an id `rec:x` is refused, and that `argv(valid, "/c.yaml", "a@b.test")` equals the list in step 1. `test_panel_runner.py`: a run of `item.move` with the email `a@b.test` spawns a command whose arguments contain `--by=a@b.test`, and the finished job has `restart_expected` false. `test_panel_api_ops.py`: posting `item.move` with a valid body returns 202 and the runner receives the session email. `test_panel_static.py`: the served `pipeline.js` contains `item.move`.
      Stop condition: if `grep -rn "_run_cli\|is_config_op" src tests` shows a caller not named in `touches`, stop with `status=blocked`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_ops.py tests/unit/test_panel_runner.py tests/unit/test_panel_api_ops.py tests/unit/test_panel_static.py -q passes"
      - "grep -n 'item.move' src/knowledge_hub/panel/ops.py src/knowledge_hub/panel/static/pipeline.js matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-docs
    title: Update the documentation and the changelog
    depends_on: [p1-regroup-sync, p2-search-embed-guard, p4-admin-move, p5-panel-move]
    complexity: M
    touches:
      - README.md
      - docs/design.md
      - docs/runbook.md
      - docs/runbook-identity.md
      - docs/configuration.md
      - docs/deployment.md
      - CHANGELOG.md
    brief: |
      Design: the whole of sections 3.0 to 3.5. Edit only these files. Every claim must match the merged code: read `src/knowledge_hub/admin/move_cmd.py` and `tools/search.py` first. Each `.md` file must stay at or under 400 lines and every relative link must resolve (`.venv/bin/python -m pytest tests/unit/test_docs_links.py -q`).
      1. `README.md`: in the Tools table remove the `move_item` row, change `ingest_record` to `Store a raw record (T3) readable by one group; hub keys only`; in "The `kh` commands" add `kh move` (`Move a record or document between groups; administrators only (--by must be in admins in kh.yaml).`); in the Tools intro keep the tool count in step with the table.
      2. `docs/design.md`: line 62 (accepted risks, the "two groups" item): remove the `ingest_record` risk and state that `propose_doc` and `mark_record_outdated` stay reachable from Claude and that `--by` is attribution only; line 110: the OAuth tool allowlist is `kh:read` gives the five reads, `kh:write` gives `propose_doc` and `mark_record_outdated`, and `ingest_record` is for hub keys only; lines 165 and 169: eight tools (five reads, `ingest_record`, `mark_record_outdated`, `propose_doc`), and `move_item` replaced by a paragraph `Moving items (kh move)` that keeps the refusals, the tombstone, the two pull requests and the "not a purge" text, and says an administrator need not hold either group, the admin caller is `admin:<email>`, no migration; lines 173-179 (Moving an item) keep the tombstone, worker-lock and "not a purge" bullets and name `kh move` instead of the tool; lines 147-149 (Search): the Search section says a query is embedded only when every group it covers may be embedded, otherwise search is full text only, `similarity` is null, and the mode is server configuration outside the cursor scope; the sync section says a sync re-reads stored documents of the same tier whose group differs from the config.
      3. `docs/runbook.md`: rewrite lines 168-184 (Moving items) around `kh move` and the panel card with the four exit codes; add the step `kh rebuild --repo <repo>` after reassigning a repo to another group (and that `kh check` names the drifted documents); in the similarity calibration section say `similarity` is null in full-text-only mode; in the accepted-risk text around line 231 (a caller holding two groups) say that `ingest_record` is for hub keys only and keep the `propose_doc` risk.
      4. `docs/runbook-identity.md:75` and `docs/configuration.md:174`: `ingest_record` is for hub keys only; `--tools move_item` gives `key: unknown tool move_item`. `docs/configuration.md`: document `kh move` in the command table and add `; run kh rebuild --repo <repo>` to the `kh check` audience line quoted at about line 182. `docs/deployment.md`: add `kh move` to the table of where each command runs (hub container and panel container; needs the database app role, the Forgejo bot token and the S3 secrets).
      5. `CHANGELOG.md` under `## [Unreleased]`: `### Changed` with the breaking entries (`move_item` is no longer an MCP tool, use `kh move` or the panel; `ingest_record` is for hub keys only and Claude connectors lose it at their next call) and `### Added` (`kh move`, the panel `Move an item` form, the `kh check` hint) and `### Fixed` (stale group after a repo reassignment; search queries sent to the http embedder for groups that may not be embedded). Do not open a version heading.
      6. `grep -rn "move_item" README.md docs` may only show historical or explanatory mentions that say the tool was removed.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - "grep -L 'kh move' README.md docs/runbook.md docs/configuration.md docs/deployment.md prints nothing"
      - "grep -c '^## \\[' CHANGELOG.md is unchanged from origin/main"
      - ".venv/bin/python -m ruff check . passes"
```
