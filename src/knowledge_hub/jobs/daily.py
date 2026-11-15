"""``kh daily`` (plan 3.12): stale flags, entity cache from Grist, expired claim ids."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

TABLES = {"Products": "prd", "Components": "cmp", "Versions": "ver", "Customers": "cus"}


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


async def run_daily(indexer_pool: Any, grist: Any, config: Any, today: date) -> dict[str, int]:
    """Idempotent. Grist is read first, so a Grist failure changes nothing."""
    audiences: dict[str, list[str]] = {}
    for kind in TABLES.values():
        auds = list(config.entities.get(kind, []))
        if not auds:  # never an open row: refuse instead of storing empty audiences
            raise ValueError(f"config entities.{kind} has no audience; refusing to cache open entities")
        audiences[kind] = auds

    rows: dict[str, tuple] = {}
    skipped = 0
    for table, kind in TABLES.items():
        for fields in await grist.fetch_table(table):
            key = _text(fields.get("key"))
            if key is None or key.partition(":")[0] != kind or not key.partition(":")[2]:
                skipped += 1
                continue
            attrs = {"links": fields.get("links")} if fields.get("links") not in (None, "") else {}
            rows[key] = (
                key, kind, _text(fields.get("name")), _text(fields.get("owner_team")),
                _text(fields.get("status")), Jsonb(json.loads(json.dumps(attrs))), audiences[kind],
            )

    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE kh.documents SET stale = (review_by < %s) "
            "WHERE review_by IS NOT NULL AND stale IS DISTINCT FROM (review_by < %s)",
            (today, today),
        )
        stale_changed = cur.rowcount
        for row in rows.values():
            await conn.execute(
                """
                INSERT INTO kh.entities_cache
                    (id, kind, name, owner_team, status, attrs, required_audiences, allowed_emails)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    kind = EXCLUDED.kind, name = EXCLUDED.name, owner_team = EXCLUDED.owner_team,
                    status = EXCLUDED.status, attrs = EXCLUDED.attrs,
                    required_audiences = EXCLUDED.required_audiences, allowed_emails = NULL,
                    synced_at = now()
                 WHERE (kh.entities_cache.kind, kh.entities_cache.name, kh.entities_cache.owner_team,
                        kh.entities_cache.status, kh.entities_cache.attrs,
                        kh.entities_cache.required_audiences, kh.entities_cache.allowed_emails)
                       IS DISTINCT FROM
                       (EXCLUDED.kind, EXCLUDED.name, EXCLUDED.owner_team, EXCLUDED.status,
                        EXCLUDED.attrs, EXCLUDED.required_audiences, EXCLUDED.allowed_emails)
                """,
                row,
            )
        cur = await conn.execute("DELETE FROM kh.entities_cache WHERE NOT (id = ANY(%s))", (list(rows),))
        removed = cur.rowcount
        cur = await conn.execute("DELETE FROM kh.claim_jti WHERE expires_at < now()")
        purged = cur.rowcount
    return {
        "stale_changed": stale_changed,
        "entities": len(rows),
        "entities_removed": removed,
        "entities_skipped": skipped,
        "jti_purged": purged,
    }
