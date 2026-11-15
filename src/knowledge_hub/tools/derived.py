"""``write_derived``: store one T2 record whose visibility is computed from its sources (plan 3.7)."""

import json
import logging
import re
import uuid
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from psycopg.types.json import Jsonb

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.tools")

KINDS = ("summary", "extracted-fact", "decision-candidate", "changelog")
MAX_SOURCES = 50
_ID = re.compile(
    r"^(doc|raw|derived):([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?:@([0-9A-Za-z._-]{1,64}))?$"
)

# Local fixed text (the catalogue has no entry): the sources share no attendee, so the record would
# be visible to nobody. Only reachable through the provenance exception.
NOBODY = "cannot write derived record: the cited sources share no attendee, so nobody could read it"

_INSERT_RECORD = """
INSERT INTO kh.derived_records (id, kind, title, body, claims, domain, generated_by, created_by_sub,
    created_by_type, required_audiences, allowed_emails)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_SOURCE = """
INSERT INTO kh.derived_sources (derived_id, source_kind, source_id, version, src_required_audiences)
VALUES (%s, %s, %s, %s, '{}')
"""


def _parse_ids(raw_ids: list[str]) -> list[tuple[str, str, str | None]]:
    """(kind, uuid, version) per distinct source, in the order given; the same source twice is one."""
    seen: dict[tuple[str, str], str | None] = {}
    for item in raw_ids:
        m = _ID.match(item)
        if m is None:
            raise kh_error("bad_id", errors.BAD_ID, x=item[:100])
        key = (m.group(1), m.group(2).lower())
        if key in seen and seen[key] != m.group(3):
            raise kh_error("bad_id", errors.BAD_ID, x=item[:100])
        seen[key] = m.group(3)
    return [(k, u, v) for (k, u), v in seen.items()]


def _claims(claims_json: str, cited: set[str]) -> list[dict[str, Any]]:
    bad = errors.KhError("bad_claims", errors.BAD_CLAIMS)
    try:
        claims = json.loads(claims_json)
    except ValueError:
        raise bad from None
    if not isinstance(claims, list):
        raise bad
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise bad
        if "quote" not in claim and "timestamp" not in claim:
            raise bad
        if not all(isinstance(claim.get(k), str) for k in ("quote", "timestamp") if k in claim):
            raise bad
        ids = claim.get("source_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise bad
        for sid in ids:
            m = _ID.match(sid)
            if m is None or f"{m.group(1)}:{m.group(2).lower()}" not in cited:
                raise bad
    return claims


async def write_derived(
    call: ToolCall,
    kind: str,
    title: str,
    body: str,
    domain: str,
    claims_json: str,
    derived_from: list[str],
    pipeline: str,
    model: str,
    pipeline_version: str,
) -> dict[str, Any]:
    deps, caller, conn = call.deps, call.caller, call.conn
    parsed = _parse_ids(derived_from)
    if not parsed:
        raise kh_error("derived_sources", errors.DERIVED_SOURCES, k=0)
    if kind not in KINDS:
        raise errors.KhError("bad_kind", errors.BAD_KIND)
    if len(parsed) > MAX_SOURCES:
        raise errors.KhError("too_many_sources", errors.TOO_MANY_SOURCES)
    if any(v is not None and k != "doc" for k, _, v in parsed):
        raise errors.KhError("bad_version", errors.BAD_VERSION)
    if domain not in deps.config.domains:
        raise errors.KhError("bad_domain", "domain must be one of " + ", ".join(deps.config.domains))

    ids = [f"{k}:{u}" for k, u, _ in parsed]
    cur = await conn.execute(
        "SELECT n_resolved, required_audiences, allowed_emails, versions"
        " FROM kh.derived_visibility(%s::text[])",
        (ids,),
    )
    n_resolved, audiences, emails, versions = await cur.fetchone()
    if n_resolved != len(ids):
        # Missing, malformed and hidden ids are indistinguishable; so is a wrong @version on a hidden doc.
        raise kh_error("derived_sources", errors.DERIVED_SOURCES, k=len(ids) - n_resolved)
    for (k, u, want), have in zip(parsed, versions, strict=True):
        if want is not None and want != have:
            raise kh_error("version_changed", errors.VERSION_CHANGED, id=f"{k}:{u}", version=have)
    claims = _claims(claims_json, set(ids))
    if not audiences and not emails:
        # Visible to nobody: only the provenance branch can produce this (a token with no shared attendee).
        raise errors.KhError("derived_nobody", NOBODY)

    derived_id = uuid.uuid4()
    generated_by = {"pipeline": pipeline, "model": model, "pipeline_version": pipeline_version}
    await conn.execute(
        _INSERT_RECORD,
        (
            derived_id,
            kind,
            title,
            body,
            Jsonb(claims),
            domain,
            Jsonb(generated_by),
            caller.sub,
            caller.principal_type,
            list(audiences),
            None if emails is None else list(emails),
        ),
    )
    for (k, u, _), have in zip(parsed, versions, strict=True):
        await conn.execute(_INSERT_SOURCE, (derived_id, k, u, have))
    try:
        # Run the deferred visibility check now so a failure is an INTERNAL error with a logged reason.
        await conn.execute("SET CONSTRAINTS kh.derived_visibility_matches IMMEDIATE")
    except psycopg.Error as exc:
        ref = uuid.uuid4()
        log.error("derived visibility check failed sqlstate=%s ref=%s: %s", exc.sqlstate, ref, exc)
        raise kh_error("internal", errors.INTERNAL, ref=ref) from None
    return {"citation_id": f"derived:{derived_id}"}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="write_derived",
        description=(
            "Store one derived (T2) record such as a summary, citing the sources it was made from "
            "(doc:, raw: or derived: ids; doc: ids may carry @<commit>). The record is visible exactly "
            "to those who may see every cited source."
        ),
    )(tool_guard("write_derived", deps)(write_derived))
