"""Read-only Grist client for the ``kh-entities`` document (plan 3.12)."""

from __future__ import annotations

from typing import Any

import httpx


class GristError(RuntimeError):
    """Grist answered with an error or an unexpected body."""


class GristClient:
    def __init__(
        self,
        url: str,
        doc_id: str,
        key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._doc_id = doc_id
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
            timeout=timeout,
        )

    async def fetch_table(self, name: str) -> list[dict[str, Any]]:
        """The ``fields`` object of every record of table ``name``."""
        try:
            resp = await self._http.get(f"/api/docs/{self._doc_id}/tables/{name}/records")
            resp.raise_for_status()
            records = resp.json()["records"]
            return [dict(r["fields"]) for r in records]
        except httpx.HTTPStatusError as exc:
            raise GristError(f"grist table {name}: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise GristError(f"grist table {name}: {exc.__class__.__name__}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise GristError(f"grist table {name}: unexpected response body") from exc

    async def aclose(self) -> None:
        await self._http.aclose()
