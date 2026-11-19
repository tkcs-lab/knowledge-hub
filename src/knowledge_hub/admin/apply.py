"""Stage, apply, commit: run one admin edit against ``kh.yaml`` and the setup steps (plan 45, 3.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from knowledge_hub.admin import yamledit
from knowledge_hub.admin.yamledit import Doc
from knowledge_hub.config import ConfigError, KhConfig
from knowledge_hub.settings import Settings, load_settings
from knowledge_hub.setup.cli import NotSet, build_steps
from knowledge_hub.setup.steps import Change, _message

if TYPE_CHECKING:
    from knowledge_hub.setup.forgejo import ForgejoStep
    from knowledge_hub.setup.labels import LabelsStep
    from knowledge_hub.setup.postgres import PostgresStep

Edit = Callable[[Doc], bool]
Check = Callable[["Steps", KhConfig], Awaitable[str | None]]
After = Callable[["Steps", bool], Awaitable[list[Change]]]

RESTART_NOTE = "restart kh serve: the hub reads kh.yaml at start"


class StepBuildError(Exception):
    """A step builder rejected its configuration (a ``ValueError``): nothing may be written."""


class Steps:
    """The real setup steps for one config, built lazily and once each."""

    def __init__(self, settings: Settings, config: KhConfig) -> None:
        self._settings = settings
        self._config = config
        self._builders = build_steps(settings, config, False)
        self._cache: dict[str, Any] = {}

    def _get(self, name: str) -> Any:
        if name not in self._cache:
            try:
                self._cache[name] = self._builders[name]()
            except ValueError as exc:
                raise StepBuildError(_message(exc)) from exc
        return self._cache[name]

    def labels(self) -> LabelsStep:
        return cast("LabelsStep", self._get("labels"))

    def postgres(self) -> PostgresStep:
        return cast("PostgresStep", self._get("postgres"))

    def forgejo(self) -> ForgejoStep:
        return cast("ForgejoStep", self._get("forgejo"))


StepsFactory = Callable[[KhConfig], Steps]


def default_steps_factory(config: KhConfig) -> Steps:
    return Steps(load_settings(), config)


def _refused(changes: Sequence[Change], target: str | None) -> bool:
    if target is None:
        return False
    return any(c.prefix == "!" and (c.text == target or c.text.startswith(target + ":")) for c in changes)


async def execute(
    path: Path | None,
    edit: Edit,
    *,
    check: Check | None = None,
    after: After | None = None,
    target: str | None = None,
    notes: Sequence[str] = (),
    dry_run: bool,
    no_apply: bool,
    steps_factory: StepsFactory | None = None,
) -> int:
    if path is None:
        print("admin: no config file: pass --config or set KH_CONFIG_FILE")
        return 2
    try:
        with yamledit.locked(path):
            return await _run(path, edit, check, after, target, notes, dry_run, no_apply, steps_factory)
    except ConfigError as exc:
        print(str(exc))
        return 2


async def _run(
    path: Path,
    edit: Edit,
    check: Check | None,
    after: After | None,
    target: str | None,
    notes: Sequence[str],
    dry_run: bool,
    no_apply: bool,
    steps_factory: StepsFactory | None,
) -> int:
    doc = yamledit.load(path)
    if not edit(doc):
        print("nothing to change")
        return 0
    new_text = yamledit.render(doc)
    config = yamledit.validate(new_text)
    print(yamledit.unified_diff(doc.text, new_text, "kh.yaml"), end="")

    applying = not no_apply and (check is not None or after is not None)
    steps: Steps | None = None
    if applying:
        try:
            steps = (steps_factory or default_steps_factory)(config)
        except NotSet as exc:
            return _not_set(exc.name, dry_run)
        except (ValueError, OSError) as exc:
            print(f"admin: {_message(exc)}")
            return 2

    if dry_run:
        if steps is None:
            return 0
        try:
            if check is not None:
                refusal = await check(steps, config)
                if refusal:
                    print(f"admin: {refusal}")
                    return 2
            if after is not None:
                for change in await after(steps, False):
                    print(change.render())
        except NotSet as exc:
            return _not_set(exc.name, True)
        except StepBuildError as exc:
            print(f"admin: {exc}")
            return 2
        except Exception as exc:
            print(f"setup: {_message(exc)}")
            return 2
        return 0

    changes: list[Change] = []
    failed = False
    if steps is not None:
        try:
            if check is not None:
                refusal = await check(steps, config)
                if refusal:
                    print(f"admin: {refusal}")
                    return 2
        except NotSet as exc:
            return _not_set(exc.name, False)
        except StepBuildError as exc:
            print(f"admin: {exc}")
            return 2
        except Exception as exc:
            print(f"setup: {_message(exc)}")
            return 2
        try:
            if after is not None:
                changes = await after(steps, True)
        except NotSet as exc:
            return _not_set(exc.name, False)
        except StepBuildError as exc:
            print(f"admin: {exc}")
            return 2
        except Exception as exc:
            failed = True
            print(f"setup: {_message(exc)}")
        for change in changes:
            print(change.render())
        if _refused(changes, target):
            print("kh.yaml unchanged")
            return 2

    yamledit.commit(path, new_text)
    code = 0
    if failed:
        print("kh.yaml changed; apply incomplete: run kh setup apply")
        code = 2
    elif no_apply and after is not None:
        print("kh.yaml changed; run kh setup apply")
    else:
        bang = sum(1 for c in changes if c.prefix == "!")
        if bang:
            print(f"kh.yaml changed; {bang} lines need your attention")
            code = 2
        else:
            print("kh.yaml updated")
    for note in notes:
        print(note)
    print(RESTART_NOTE)
    return code


def _not_set(name: str, dry_run: bool) -> int:
    if dry_run:
        print(f"plan skipped: {name} is not set")
        return 0
    print(f"admin: {name} is not set")
    return 2
