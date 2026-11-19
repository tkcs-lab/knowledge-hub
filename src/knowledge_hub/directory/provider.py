"""The directory provider interface (plan 58, 3.5). The core never imports a provider module."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import httpx

    from knowledge_hub.config import KhConfig
    from knowledge_hub.directory.model import GroupFetch
    from knowledge_hub.settings import Settings

PROVIDERS = {
    "google": "knowledge_hub.directory.google",
    "keycloak": "knowledge_hub.directory.keycloak",
}


@dataclass(frozen=True)
class GroupRefs:
    members: tuple[str, ...]
    approvers: tuple[str, ...]


class ProviderError(Exception):
    """Holds the endpoint name and status code only, never a response body, an assertion or a secret."""


class DirectoryProvider(Protocol):
    name: str

    async def fetch(self, refs: Mapping[str, GroupRefs]) -> dict[str, GroupFetch]:
        """Every requested group, or ProviderError; never an empty list for a failure."""
        ...

    async def aclose(self) -> None: ...


def build_provider(
    name: str, settings: Settings, *, http: httpx.AsyncClient | None = None
) -> DirectoryProvider:
    module = PROVIDERS.get(name)
    if module is None:
        raise ValueError(f"directory: unknown provider {name}")
    provider: DirectoryProvider = importlib.import_module(module).make(settings, http)
    return provider


def refs_from_config(config: KhConfig, provider: str) -> dict[str, GroupRefs]:
    out: dict[str, GroupRefs] = {}
    for name in config.active_groups:
        entry = config.groups[name].directory.get(provider)
        if entry is not None and (entry.members or entry.approvers):
            out[name] = GroupRefs(tuple(entry.members), tuple(entry.approvers))
    return out
