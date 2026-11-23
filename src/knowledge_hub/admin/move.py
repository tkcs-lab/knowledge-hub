"""Move a record or a document between groups: the core of kh move (plan 69)."""

import hashlib
import logging
import uuid
from typing import Any

import psycopg

from knowledge_hub.forgejo import ForgejoError
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error
from knowledge_hub.propose_render import check_text, render_file, validate_path
from knowledge_hub.tools.docs import parse_id

log = logging.getLogger("knowledge_hub.tools")

NOT_HELD_SQLSTATE = "P0K08"
WRONG_GROUP_SQLSTATE = "P0K09"
LINKED_SQLSTATE = "P0K0A"
DATASET_SQLSTATE = "P0K0C"

TITLE_MAX = 120

async def _move_record(call: ToolCall, key: str, from_group: str, to_group: str) -> dict[str, Any]:
    try:
        async with call.conn.transaction():  # a savepoint: the function's refusal must not abort the call
            async with call.conn.cursor() as cur:
                await cur.execute("SELECT kh.move_record(%s, %s, %s)", (key, from_group, to_group))
                row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise kh_error("move_conflict", errors.MOVE_CONFLICT) from None
    except psycopg.errors.ForeignKeyViolation:
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD) from None
    except psycopg.errors.Error as exc:
        if exc.sqlstate == NOT_HELD_SQLSTATE:
            raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD) from None
        if exc.sqlstate == WRONG_GROUP_SQLSTATE:
            raise kh_error("move_wrong_group", errors.MOVE_WRONG_GROUP) from None
        if exc.sqlstate == LINKED_SQLSTATE:
            raise kh_error("move_linked", errors.MOVE_LINKED) from None
        if exc.sqlstate == DATASET_SQLSTATE:
            raise kh_error("dataset_record", errors.DATASET_RECORD) from None
        raise
    if row is None or row[0] is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    rid = f"rec:{key}"
    _log(call.caller.sub, rid, from_group, to_group)
    return {"tier": "T3", "id": rid, "from_group": from_group, "to_group": to_group, "moved": True}


def git_blob_sha(text: str) -> str:
    """The git blob hash of ``text`` encoded as UTF-8."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git's own object id


def _marked(pulls: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    for pull in pulls:
        if marker in (pull.get("body") or "").splitlines():
            return pull
    return None


async def _move_document(call: ToolCall, key: str, from_group: str, to_group: str) -> dict[str, Any]:
    deps, caller = call.deps, call.caller
    bot = deps.forgejo_bot
    if bot is None:
        raise kh_error("not_configured", errors.NOT_CONFIGURED)
    async with call.conn.cursor() as cur:
        await cur.execute(
            "SELECT id, repo, path, doc_key, audience, title, body, labels, review_by, blob_sha, "
            "supersedes, superseded_by FROM kh.documents WHERE id = %s",
            (key,),
        )
        row = await cur.fetchone()
        if row is None:
            raise kh_error("not_found", errors.NOT_FOUND)
        (
            _,
            from_repo,
            path,
            doc_key,
            audience,
            title,
            body,
            labels,
            review_by,
            row_blob,
            supersedes,
            superseded_by,
        ) = row
        if audience != from_group:
            raise kh_error("move_wrong_group", errors.MOVE_WRONG_GROUP)
        doc_id = f"doc:{key}"
        await cur.execute("SELECT 1 FROM kh.records WHERE replaced_by = %s", (doc_id,))
        if superseded_by is not None or await cur.fetchone() is not None:
            raise kh_error("move_linked", errors.MOVE_LINKED)
        await cur.execute(
            "SELECT doc_key FROM kh.documents WHERE superseded_by = %s ORDER BY doc_key", (key,)
        )
        revived = [r[0] for r in await cur.fetchall()]
    to_repo = deps.config.repo_for_group(to_group)
    labels = list(labels)
    review = review_by.isoformat()
    refuse_text = kh_error("move_text", errors.MOVE_TEXT)
    if not (validate_path(path) and check_text(title) and check_text(body)):
        raise refuse_text
    text = render_file(doc_key, title, review, labels, body, [])
    back = parse_document(text, path)
    if (
        not isinstance(back, ParsedDoc)
        or (back.id, back.title, back.review_by.isoformat(), back.labels, back.body)
        != (doc_key, title, review, labels, body)
        or back.supersedes != []
    ):
        raise refuse_text
    if len(text.encode()) > deps.settings.propose_max_bytes:
        raise kh_error("too_large", errors.TOO_LARGE)
    if set(labels) & deps.config.archived_label_set:
        raise kh_error("bad_label", errors.BAD_LABEL)

    marker = f"Moved-from: {doc_id}"
    trailer = (
        f"Requested-by: {caller.sub}\nPrincipal: {caller.principal_type}\n"
        f"Written-by: {deps.config.bot_login}\n{marker}"
    )
    result: dict[str, Any] = {
        "tier": "T0",
        "id": doc_id,
        "from_group": from_group,
        "to_group": to_group,
        "add_pr_url": "",
        "add_pr_number": 0,
        "remove_pr_url": "",
        "remove_pr_number": 0,
        "dropped_supersedes": list(supersedes),
        "revived": revived,
        "note": "",
    }
    try:
        if await bot.blob_sha(from_repo, path, "main") != row_blob:
            raise kh_error("blob_changed", errors.BLOB_CHANGED)
        to_pulls = await bot.list_open_pulls(to_repo, deps.config.bot_login)
        from_pulls = await bot.list_open_pulls(from_repo, deps.config.bot_login)
        add_open = _marked(to_pulls, marker)
        remove_open = _marked(from_pulls, marker)
        add_merged = False
        logged = False
        if add_open is not None:
            result["add_pr_url"], result["add_pr_number"] = add_open["html_url"], add_open["number"]
            if remove_open is not None:
                result["remove_pr_url"], result["remove_pr_number"] = (
                    remove_open["html_url"],
                    remove_open["number"],
                )
                return result
        elif remove_open is not None:
            raise kh_error("move_conflict", errors.MOVE_CONFLICT)
        elif await bot.blob_sha(to_repo, path, "main") == git_blob_sha(text):
            add_merged = True
        else:
            if await bot.blob_sha(to_repo, path, "main") is not None:
                raise kh_error("move_conflict", errors.MOVE_CONFLICT)
            async with call.conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM kh.documents WHERE repo = %s AND doc_key = %s", (to_repo, doc_key)
                )
                if await cur.fetchone() is not None:
                    raise kh_error("move_conflict", errors.MOVE_CONFLICT)
        limit = deps.settings.propose_max_open
        if (add_open is None and not add_merged and len(to_pulls) >= limit) or len(from_pulls) >= limit:
            raise kh_error("too_many_open", errors.TOO_MANY_OPEN)

        if add_open is None and not add_merged:
            branch = f"bot/move/{uuid.uuid4()}"
            await bot.create_branch(to_repo, branch, await bot.head_sha(to_repo, "main"))
            await bot.put_file(
                to_repo, branch, path, text, f"Move in: {title}\n\nRequested-by: {caller.sub}", None
            )
            pull = await bot.open_pull(
                to_repo,
                branch,
                "main",
                f"Move in: {title}"[:TITLE_MAX],
                f"Moved from group `{from_group}`.\n\n{trailer}",
            )
            result["add_pr_url"], result["add_pr_number"] = pull["html_url"], pull["number"]
            _log(caller.sub, doc_id, from_group, to_group)
            logged = True
    except (ForgejoError, KeyError, TypeError) as exc:
        log.warning("move_item failed: %s", exc)
        raise kh_error("propose_failed", errors.PROPOSE_FAILED) from None

    lead = (
        "The add pull request is already merged."
        if add_merged
        else f"Merge only after {result['add_pr_url']} is merged; until then the document is missing "
        "from both groups' index."
    )
    try:
        branch = f"bot/move/{uuid.uuid4()}"
        await bot.create_branch(from_repo, branch, await bot.head_sha(from_repo, "main"))
        await bot.delete_file(
            from_repo, branch, path, f"Move out: {title}\n\nRequested-by: {caller.sub}", row_blob
        )
        pull = await bot.open_pull(
            from_repo, branch, "main", f"Move out: {title}"[:TITLE_MAX], f"{lead}\n\n{trailer}"
        )
        result["remove_pr_url"], result["remove_pr_number"] = pull["html_url"], pull["number"]
        if not logged:
            _log(caller.sub, doc_id, from_group, to_group)
    except (ForgejoError, KeyError, TypeError) as exc:
        log.warning("move_item removal failed: %s", exc)
        result["note"] = errors.MOVE_PARTIAL
    return result


def _log(sub: str, item: str, from_group: str, to_group: str) -> None:
    log.info("move_item sub=%s id=%s from=%s to=%s", sub, item, from_group, to_group)


async def move_item(call: ToolCall, id: str, from_group: str, to_group: str) -> dict[str, Any]:
    caller, deps = call.caller, call.deps
    if from_group == to_group:
        raise kh_error("bad_arg", errors.BAD_ARG, name="to_group")
    for group in (from_group, to_group):
        if group not in caller.audiences or not deps.config.is_active(group):
            raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    parsed = parse_id(id)
    if parsed is None:
        raise kh_error("bad_id", errors.BAD_ID)
    if parsed[0] == "rec":
        return await _move_record(call, str(parsed[1]), from_group, to_group)
    return await _move_document(call, str(parsed[1]), from_group, to_group)

