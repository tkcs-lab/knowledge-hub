"""Watch kh.yaml and tell the server when a valid, different config is on disk (plan 3.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from knowledge_hub.config import ConfigError, KhConfig, load_config

log = logging.getLogger(__name__)

EXIT_RESTART = 75


def config_digest(config: KhConfig) -> str:
    """SHA-256 of the config without ``admins``; independent of key order and formatting."""
    data = config.model_dump(mode="json", exclude={"admins"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class ConfigWatcher:
    def __init__(
        self, path: Path, loaded: KhConfig, load: Callable[[Path], KhConfig] = load_config
    ) -> None:
        self._path = path
        self._load = load
        self._loaded = config_digest(loaded)
        self._pending: str | None = None
        self._last_error: str | None = None

    def poll(self) -> bool:
        try:
            config = self._load(self._path)
        except (ConfigError, OSError) as exc:
            text = str(exc)
            if text != self._last_error:
                log.warning("config on disk is invalid; keeping the loaded config: %s", text)
                self._last_error = text
            self._pending = None
            return False
        self._last_error = None
        digest = config_digest(config)
        if digest == self._loaded:
            self._pending = None
            return False
        if self._pending == digest:
            return True
        self._pending = digest
        return False


async def watch(
    path: Path,
    loaded: KhConfig,
    interval: float,
    on_change: Callable[[], Any],
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    load: Callable[[Path], KhConfig] = load_config,
) -> None:
    watcher = ConfigWatcher(path, loaded, load)
    while True:
        await sleep(interval)
        if watcher.poll():
            log.info("config changed on disk; exiting with code %d so the container restarts", EXIT_RESTART)
            on_change()
            return
