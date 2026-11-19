"""Shared state of the panel app: settings, signer, upstream client and config loader."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from knowledge_hub.config import KhConfig
from knowledge_hub.panel.session import Signer
from knowledge_hub.panel.settings import PanelSettings


@dataclass
class PanelContext:
    settings: PanelSettings
    signer: Signer | None
    upstream: Any | None
    load_config: Callable[[], KhConfig]
    pool: Any | None = None
    hub_settings: Any | None = None
    now: Callable[[], float] = time.time

    @property
    def secure(self) -> bool:
        return (self.settings.public_url or "").startswith("https://")

    def cookie_name(self, base: str) -> str:
        return "__Host-" + base if self.secure else base
