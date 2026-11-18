"""Round-trip editing of ``kh.yaml`` (plan 45, 3.4): comments survive, writes are atomic."""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import io
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from knowledge_hub.config import ConfigError, KhConfig, parse_config


@dataclass
class Doc:
    data: Any
    text: str


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(path: str | Path) -> Doc:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"config: cannot read {path}: {exc.strerror}") from exc
    try:
        data = _yaml().load(text)
    except YAMLError as exc:
        raise ConfigError(f"config: invalid YAML in {path}") from exc
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError("config: top level must be a mapping")
    return Doc(data=data, text=text)


def render(doc: Doc) -> str:
    buf = io.StringIO()
    _yaml().dump(doc.data, buf)
    return buf.getvalue()


def validate(text: str) -> KhConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError("config: invalid YAML") from exc
    if data is not None and not isinstance(data, dict):
        raise ConfigError("config: top level must be a mapping")
    return parse_config(data)


@contextlib.contextmanager
def locked(path: str | Path) -> Iterator[None]:
    p = Path(path)
    lock_path = p.parent / f".{p.name}.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ConfigError(f"admin: cannot lock {path}: {exc.strerror}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConfigError(f"admin: cannot lock {path}: {exc.strerror}") from exc
        yield
    finally:
        os.close(fd)


def commit(path: str | Path, text: str) -> None:
    target = Path(os.path.realpath(path))
    tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        try:
            st = target.stat()
        except FileNotFoundError:
            st = None
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            if st is not None:
                os.chmod(tmp, st.st_mode & 0o7777)
                with contextlib.suppress(OSError):
                    os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        raise ConfigError(f"admin: cannot write {path}: {exc.strerror}") from exc


def unified_diff(old: str, new: str, name: str = "kh.yaml") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=name,
            tofile=f"{name} (new)",
            n=3,
        )
    )
