# Feeding a dataset from n8n

A dataset is a typed table that the hub owns. An n8n workflow keeps its rows in step with a source system through three hub tools. This guide covers the setup, the two kinds of run and a reference recipe for Salesforce. The recipe is the only Salesforce-specific part: another source needs a different export step and mapping, and nothing else changes.

## Set up the dataset and the key

An administrator (`--by` must be in `admins` of `kh.yaml`) defines the dataset and its columns. Groups must be active; datasets cannot be renamed or dropped.

```bash
kh dataset create sf_opportunities --title "Opportunities" --audiences sales --by admin@example.com
kh dataset add-column sf_opportunities name --type text --embed --by admin@example.com
kh dataset add-column sf_opportunities amount --type number --filterable --source-name Amount --by admin@example.com
kh dataset add-column sf_opportunities stage --type choice --choices Open,Won,Lost --filterable --by admin@example.com
kh dataset set sf_opportunities --title-column name --by admin@example.com
```

Types are `text`, `number`, `date`, `bool`, `choice` and `ref` (`--ref <dataset>` names the target; a ref resolves only inside the row's own group). `--embed` puts a column in the search text, `--filterable` lets `query` filter and group by it, `--sensitive` keeps it out of search and the embedder (it still reaches `get_doc`, `query` and BI logins), and `--indexed` adds a database index. See [configuration](configuration.md), "Commands", and the runbook section [Datasets](runbook.md#datasets).

Create a hub key for the workflow. It holds one group and only the tools the feed needs:

```bash
kh key create --group sales --tools list_datasets,upsert_rows,finish_sync,delete_rows --name sf-opportunities-feed --out feed.key
```

The file holds the line `key: khk_<id>.<secret>`; store the part after `key: ` as a header credential in n8n (see [identity runbook](runbook-identity.md), "Hub keys"). The three write tools are for hub keys only: any other caller gets `this tool is available to workflows only`.

## The tools

| Tool | Arguments | Use |
|---|---|---|
| `list_datasets` | `dataset` (optional) | The datasets the key may see with their columns, `watermark` (the newest `source_updated_at` of its group) and `last_syncs`. Name one dataset to get its `rows` count too. |
| `upsert_rows` | `dataset`, `audience`, `rows`, `sync_id` (optional) | Insert or update up to 500 rows. |
| `finish_sync` | `dataset`, `audience`, `sync_id`, `force` (optional, default false) | End a full sync. |
| `delete_rows` | `dataset`, `audience`, `keys` | Mark up to 500 rows outdated by key. |

`audience` is the key's group and must be in the dataset's `audiences`, otherwise the answer is `unknown dataset`.

`rows` is a JSON string holding an array of objects with exactly the keys `key`, `source_updated_at` and `fields`:

```json
[{"key": "006Qx0000012AbC", "source_updated_at": "2026-11-23T08:15:00Z",
  "fields": {"name": "Acme renewal", "Amount": 12000, "stage": "Open"}}]
```

- `key` is 1 to 200 characters and identifies the row inside the group. Reusing a key updates the row.
- `source_updated_at` is ISO 8601 with a time zone. A row with an older timestamp than the stored one leaves the stored values; an equal or newer one wins.
- `fields` names columns by column name or by `source_name`, as set on the column. Every row of one call must use the same set of fields; a column that is not in the set keeps its stored value on update and is null on insert.
- Empty or null gives null. Numbers may be JSON numbers or numeric strings; dates are `YYYY-MM-DD` or an ISO datetime; a choice must be one of its choices.

A bad row stops the call with `row <index>: <problem>` (for example `row 3: amount is not a valid number`); nothing of that call is written. More than 500 rows or keys gives `at most 500 rows or keys per call`. The body of a call must also stay under `KH_MAX_BODY_BYTES` (1 MiB by default), so split a batch of wide rows earlier than 500.

## Incremental runs (hourly)

1. Call `list_datasets` with `dataset` set and read `watermark` of the dataset.
2. Ask the source for rows changed at or after the watermark (`>=`, not `>`, so rows with the same timestamp are not missed; an upsert of an unchanged row is harmless).
3. Sort ascending by the source's change time and send batches of at most 500 rows, each under `KH_MAX_BODY_BYTES`, to `upsert_rows` **without** `sync_id`.

If a source deletes a row, send its key to `delete_rows`; the row leaves search and `query`, and an upsert with a newer `source_updated_at` brings it back.

## Full runs (weekly)

A full run re-sends every row and then removes the ones the source no longer has.

1. Choose a `sync_id` such as `full-20261123` (letters, digits and `. _ : -`, up to 64 characters), the same for every batch of the run.
2. Send every row in batches of at most 500 to `upsert_rows` with that `sync_id`.
3. Call `finish_sync` with the same `sync_id`. Rows of the group that were not sent since the sync started are marked outdated. Incremental runs in between do not break this: a row they touch after the sync started stays live.

A finished `sync_id` cannot be reused. `finish_sync` refuses when it would outdate more than the dataset's `max_outdated_percent` (default 20) of the live rows: `finish_sync would mark N of M rows outdated, more than P percent; repeat with force=true if that is right`. Check the export first; a truncated export is the usual cause. Repeat with `force=true` only when the loss is real, and raise the limit with `kh dataset set <dataset> --max-outdated-percent N` when the source legitimately shrinks.

## Reference recipe: Salesforce to `sf_opportunities`

Two workflows (or one with two schedule triggers): hourly incremental and weekly full. Each has the same four steps.

1. **Schedule trigger.** Hourly for the incremental run, weekly for the full run.
2. **Hub MCP Client node, `list_datasets`** with the hub key and `dataset = sf_opportunities`; keep `watermark` (incremental runs only).
3. **MCP Client node with a PrivacyFence scoped token.** The token allows only the read-only `salesforce_export_report` tool, so the workflow cannot write to Salesforce. Filter the report by `LAST_UPDATE >= <watermark>` for an incremental run; a full run exports the whole report.
4. **Code node.** Map each exported record to `{key: <Id>, source_updated_at: <LAST_UPDATE>, fields: {...}}` and emit the rows in chunks of 500, as one JSON string per chunk.
5. **MCP Client node with the hub key**, calling `upsert_rows` once per chunk: `dataset`, `audience`, `rows`, and for a full run `sync_id = full-<yyyymmdd>`. After the last chunk of a full run, call `finish_sync`.

Salesforce is only steps 3 and 4. The dataset, the key, the batching, the watermark and the sync rule are the same for any source. For a one-off load from a file use `kh dataset import` instead (see the runbook).

## When something goes wrong

| Answer | Meaning |
|---|---|
| `unknown dataset` | The dataset does not exist, or its `audiences` do not list the key's group. |
| `row <i>: <problem>` | Row `i` of the call (counted from 0) is invalid; fix it and resend the batch. |
| `argument sync_id is invalid` | The `sync_id` is empty for `finish_sync`, malformed, already finished or not started. |
| `finish_sync would mark ...` | The shrink guard; see above. |

Search finds a row as a T3 hit with a `row` id; `get_doc` opens `row:<dataset>:<uuid>`; `query` and `save_query` build tables and charts; `query(as_of=...)` reads history when the dataset keeps it (`kh dataset set <dataset> --history`).
