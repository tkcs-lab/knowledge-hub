"""The step protocol, the change line and the runner of ``kh setup`` (plan 3.7)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Prefix = Literal["+", "~", "-", "!", "?", "i"]
DRIFT: frozenset[str] = frozenset({"+", "~", "-", "!"})
_SECRET = re.compile(r"(?i)(password|token|secret)\s*=\s*('[^']*'|\S+)")


@dataclass(frozen=True)
class Change:
    prefix: Prefix
    text: str

    def render(self) -> str:
        return f"{self.prefix} {self.text}"


class Step(Protocol):
    name: str

    async def plan(self) -> list[Change]: ...

    async def apply(self, prune: bool) -> list[Change]: ...


def _message(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return _SECRET.sub(r"\1=***", text)


async def run(steps: Sequence[Step], mode: Literal["plan", "apply"], prune: bool = False) -> int:
    """Run the steps in order, print every line, return the exit code of plan 3.7.

    A step that raises prints ``setup: <step>: <message>``; the later steps are skipped
    because they may depend on it, and the run returns 2.
    """
    drift = False
    refused = False
    for step in steps:
        try:
            changes = await (step.plan() if mode == "plan" else step.apply(prune))
        except Exception as exc:
            print(f"setup: {step.name}: {_message(exc)}", flush=True)
            return 2
        for change in changes:
            print(change.render(), flush=True)
            if change.prefix in DRIFT:
                drift = True
            if change.prefix == "!":
                refused = True
    if mode == "plan":
        return 1 if drift else 0
    return 2 if refused else 0
