"""A frontmatter id may repeat in two repos; get_doc takes the row uuid, so nothing leaks or breaks."""

import uuid

import psycopg

from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, carol_hr

NOT_FOUND = "not found"


def _twin(fresh_db, original_id) -> str:
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        conn.execute("CREATE TEMP TABLE twin AS SELECT * FROM kh.documents WHERE id = %s", (original_id,))
        conn.execute(
            "UPDATE twin SET id = gen_random_uuid(), repo = 'canonical-hr', path = 'docs/hr-twin.md', "
            "content_sha = 'twin', required_audiences = '{hr}'"
        )
        row = conn.execute("INSERT INTO kh.documents SELECT * FROM twin RETURNING id").fetchone()
        keys = conn.execute(
            "SELECT doc_key FROM kh.documents WHERE id IN (%s, %s)", (original_id, row[0])
        ).fetchall()
    assert {k[0] for k in keys} == {"doc-all"} and len(keys) == 2
    return str(row[0])


async def test_same_doc_id_in_two_repos(seeded_db, fresh_db, mcp_client):
    original = seeded_db["documents"]["all"]
    twin = _twin(fresh_db, original)
    missing = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{uuid.uuid4()}"})
    hidden = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{twin}"})
    assert hidden.is_error and hidden.text.endswith(NOT_FOUND)
    assert (hidden.status, hidden.is_error, hidden.text, hidden.data) == (
        missing.status,
        missing.is_error,
        missing.text,
        missing.data,
    )
    shown = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{original}"})
    assert not shown.is_error
    dumped = str(shown.data) + shown.text
    assert "canonical-hr" not in dumped and twin not in dumped
    own = await mcp_client.call(carol_hr, "get_doc", {"id": f"doc:{twin}"})
    assert not own.is_error
