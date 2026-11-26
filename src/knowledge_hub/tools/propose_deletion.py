"""``propose_doc_deletion``: open a pull request that deletes one T0 document."""

import logging
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.forgejo import ForgejoError
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.propose_render import check_text
from knowledge_hub.tools.docs import parse_id

log = logging.getLogger("knowledge_hub.tools")

TITLE_MAX = 120


async def propose_doc_deletion(
    call: ToolCall,
    id: str,  # noqa: A002 - the tool argument is named id
    summary: str = "",
) -> dict[str, Any]:
    deps, caller = call.deps, call.caller
    bot = deps.forgejo_bot
    if bot is None:
        raise kh_error("not_configured", errors.NOT_CONFIGURED)
    parsed = parse_id(id)
    if parsed is None or parsed[0] != "doc":
        raise kh_error("bad_arg", errors.BAD_ARG, name="id")
    doc_id = str(parsed[1])
    if not check_text(summary):
        raise kh_error("bad_text", errors.BAD_TEXT)
    if len(summary.encode()) > deps.settings.propose_max_bytes:
        raise kh_error("too_large", errors.TOO_LARGE)

    async with call.conn.cursor() as cur:
        await cur.execute(
            "SELECT repo, path, audience, title, blob_sha, tier, superseded_by "
            "FROM kh.documents WHERE id = %s",
            (doc_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise kh_error("not_found", errors.NOT_FOUND)
        repo, path, audience, title, blob_sha, tier, superseded_by = row
        if tier != "T0":
            raise kh_error("delete_not_t0", errors.DELETE_NOT_T0)
        if superseded_by is not None:
            raise kh_error("delete_linked", errors.DELETE_LINKED)
        if audience not in caller.audiences or not deps.config.is_active(audience):
            raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
        await cur.execute("SELECT 1 FROM kh.records WHERE replaced_by = %s", (f"doc:{doc_id}",))
        if await cur.fetchone() is not None:
            raise kh_error("delete_linked", errors.DELETE_LINKED)
        await cur.execute("SELECT id FROM kh.documents WHERE superseded_by = %s ORDER BY doc_key", (doc_id,))
        revived = [f"doc:{r[0]}" for r in await cur.fetchall()]

    marker = f"Deletes: doc:{doc_id}"
    try:
        if await bot.blob_sha(repo, path, "main") != blob_sha:
            raise kh_error("blob_changed", errors.BLOB_CHANGED)
        pulls = await bot.list_open_pulls(repo, deps.config.bot_login)
        if any(marker in (pull.get("body") or "").splitlines() for pull in pulls):
            raise kh_error("delete_pending", errors.DELETE_PENDING)
        if len(pulls) >= deps.settings.propose_max_open:
            raise kh_error("too_many_open", errors.TOO_MANY_OPEN)
        branch = f"bot/delete/{uuid.uuid4()}"
        await bot.create_branch(repo, branch, await bot.head_sha(repo, "main"))
        await bot.delete_file(
            repo, branch, path, f"Delete: {title}\n\nRequested-by: {caller.sub}", blob_sha
        )
        trailer = (
            f"Requested-by: {caller.sub}\nPrincipal: {caller.principal_type}\n"
            f"Written-by: {deps.config.bot_login}\n{marker}"
        )
        body = f"{summary}\n\n{trailer}" if summary else trailer
        if revived:
            body += "\n\nRevives: " + ", ".join(f"`{r}`" for r in revived)
        pull = await bot.open_pull(repo, branch, "main", f"Delete: {title}"[:TITLE_MAX], body)
        return {"pr_url": pull["html_url"], "pr_number": pull["number"], "branch": branch, "revived": revived}
    except (ForgejoError, KeyError, TypeError) as exc:
        log.warning("propose_doc_deletion failed: %s", exc)
        raise kh_error("propose_failed", errors.PROPOSE_FAILED) from None


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="propose_doc_deletion",
        description=(
            "Propose deleting a T0 document of a group you hold, as a pull request that its approvers "
            "review. Pass the document id (doc:<uuid>) from search or get_doc. The answer lists revived: "
            "documents that become current again when it is merged. Nothing changes until an approver merges."
        ),
    )(tool_guard("propose_doc_deletion", deps)(propose_doc_deletion))
