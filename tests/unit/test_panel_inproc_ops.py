"""``run_inproc`` paths that need no database."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_hub.config import ConfigError
from knowledge_hub.panel.ops import OpError
from knowledge_hub.panel.pipeline_ops import run_inproc

pytestmark = pytest.mark.unit


async def test_rebuild_with_a_broken_config_is_an_op_error():
    def load():
        raise ConfigError("kh.yaml: bad")

    ctx = SimpleNamespace(pool=object(), load_config=load)
    with pytest.raises(OpError, match="kh.yaml: bad"):
        await run_inproc("repo.rebuild", {"repo": "kb-all"}, ctx)


@pytest.mark.parametrize(("n", "text"), [(0, "0 jobs"), (1, "1 jobs"), (3, "3 jobs")])
async def test_output_uses_the_plan_text(monkeypatch, n, text):
    async def count(pool, *a):
        return n

    monkeypatch.setattr("knowledge_hub.panel.pipeline_ops.retry_jobs", count)
    monkeypatch.setattr("knowledge_hub.panel.pipeline_ops.reclaim_stuck", count)
    ctx = SimpleNamespace(pool=object())
    assert await run_inproc("jobs.retry", {}, ctx) == f"retried {text}"
    assert await run_inproc("jobs.reclaim", {}, ctx) == f"reclaimed {text}"
