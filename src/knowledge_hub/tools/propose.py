"""``propose_doc``: open a pull request that proposes one document (plan 3.8)."""

import datetime as dt
import logging
import re
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.config import LABEL_RE
from knowledge_hub.forgejo import ForgejoError
from knowledge_hub.indexer.frontmatter import ID_RE, MAX_SUPERSEDES, ParsedDoc, parse_document
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.propose_render import check_text, render_file, validate_path

log = logging.getLogger("knowledge_hub.tools")

TITLE_MAX = 120
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _bad(name: str) -> Exception:
    return kh_error("bad_arg", errors.BAD_ARG, name=name)


async def propose_doc(
    call: ToolCall,
    group: str,
    path: str,
    id: str,  # noqa: A002 - the tool argument is named id
    title: str,
    review_by: str,
    body: str,
    labels: list[str] = [],  # noqa: B006 - the guard reads the default; it is never mutated
    summary: str = "",
    base_blob: str = "",
    supersedes: list[str] = [],  # noqa: B006 - the guard reads the default; it is never mutated
) -> dict[str, Any]:
    deps, caller = call.deps, call.caller
    bot = deps.forgejo_bot
    if bot is None:
        raise kh_error("not_configured", errors.NOT_CONFIGURED)
    if group not in caller.audiences or group not in deps.config.groups:
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    repo = deps.config.repo_for_group(group)
    if not validate_path(path):
        raise _bad("path")
    if not ID_RE.fullmatch(id):
        raise _bad("id")
    try:
        if not _DATE_RE.fullmatch(review_by):
            raise ValueError
        dt.date.fromisoformat(review_by)
    except ValueError:
        raise _bad("review_by") from None
    if not title.strip() or "\n" in title:
        raise _bad("title")
    wanted = list(dict.fromkeys(labels))
    if not all(check_text(t) for t in (title, body, summary)):
        raise kh_error("bad_text", errors.BAD_TEXT)
    if not all(LABEL_RE.fullmatch(label) for label in wanted):
        raise kh_error("bad_label", errors.BAD_LABEL)
    if wanted:
        async with call.conn.cursor() as cur:
            await cur.execute("SELECT name FROM kh.labels WHERE name = ANY(%s)", (wanted,))
            known = {r[0] for r in await cur.fetchall()}
        if known != set(wanted):
            raise kh_error("bad_label", errors.BAD_LABEL)
    if (
        len(supersedes) > MAX_SUPERSEDES
        or len(set(supersedes)) != len(supersedes)
        or id in supersedes
        or not all(ID_RE.fullmatch(s) for s in supersedes)
    ):
        raise _bad("supersedes")
    text = render_file(id, title, review_by, wanted, body, supersedes)
    if len(text.encode()) + len(summary.encode()) > deps.settings.propose_max_bytes:
        raise kh_error("too_large", errors.TOO_LARGE)
    back = parse_document(text, path)
    if (
        not isinstance(back, ParsedDoc)
        or (back.id, back.title, back.review_by.isoformat(), back.labels, back.body)
        != (id, title, review_by, wanted, body)
        or back.supersedes != supersedes
    ):
        raise kh_error("bad_text", errors.BAD_TEXT)

    try:
        open_pulls = await bot.list_open_pulls(repo, deps.config.bot_login)
        if len(open_pulls) >= deps.settings.propose_max_open:
            raise kh_error("too_many_open", errors.TOO_MANY_OPEN)
        live = await bot.blob_sha(repo, path, "main")
        if not base_blob:
            if live is not None:
                raise kh_error("path_exists", errors.PATH_EXISTS)
            prev = None
        else:
            if live != base_blob:
                raise kh_error("blob_changed", errors.BLOB_CHANGED)
            prev = base_blob
        async with call.conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM kh.documents WHERE repo = %s AND doc_key = %s AND path <> %s",
                (repo, id, path),
            )
            if await cur.fetchone() is not None:
                raise kh_error("id_in_use", errors.ID_IN_USE)
            if supersedes:
                await cur.execute(
                    "SELECT doc_key FROM kh.documents WHERE repo = %s AND doc_key = ANY(%s)",
                    (repo, supersedes),
                )
                if {r[0] for r in await cur.fetchall()} != set(supersedes):
                    raise _bad("supersedes")
        branch = f"bot/propose/{uuid.uuid4()}"
        head = await bot.head_sha(repo, "main")
        await bot.create_branch(repo, branch, head)
        message = f"Propose: {title}\n\nRequested-by: {caller.sub}"
        await bot.put_file(repo, branch, path, text, message, prev)
        trailer = (
            f"Requested-by: {caller.sub}\nPrincipal: {caller.principal_type}\n"
            f"Written-by: {deps.config.bot_login}"
        )
        pr_body = f"{summary}\n\n{trailer}" if summary else trailer
        if supersedes:
            pr_body += "\n\nSupersedes: " + ", ".join(f"`{s}`" for s in supersedes)
        pull = await bot.open_pull(repo, branch, "main", f"Propose: {title}"[:TITLE_MAX], pr_body)
        return {"pr_url": pull["html_url"], "pr_number": pull["number"], "branch": branch}
    except (ForgejoError, KeyError, TypeError) as exc:
        log.warning("propose_doc failed: %s", exc)
        raise kh_error("propose_failed", errors.PROPOSE_FAILED) from None


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="propose_doc",
        description=(
            "Propose a new or changed document in the repository of a group you hold, as a pull request "
            "that its approvers review. To change an existing file pass its current blob_sha as "
            "base_blob; leave it empty to create a new file. review_by is YYYY-MM-DD. "
            "To change an existing document keep its supersedes list: "
            "pass the supersedes that get_doc returned."
        ),
    )(tool_guard("propose_doc", deps)(propose_doc))
