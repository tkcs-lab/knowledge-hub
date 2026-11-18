"""``LabelsStep`` also queues a git_push job per tech repo after a new label (plan 3.8)."""

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig, TechRepoConfig
from knowledge_hub.setup.labels import LabelsStep

pytestmark = pytest.mark.integration

DB = "kh_fresh"


def config(labels):
    return KhConfig(
        people={"alice": "a@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
        labels=labels,
        tech_org="kh-tech",
        tech_repos={"docs-b": TechRepoConfig(), "docs-a": TechRepoConfig()},
    )


def jobs(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_migrator", DB)) as conn:
        return conn.execute(
            "SELECT repo FROM kh.index_jobs WHERE kind = 'git_push' ORDER BY repo"
        ).fetchall()


async def test_new_label_queues_a_git_push_for_each_tech_repo(fresh_db):
    step = LabelsStep(config({"ember": "Ember"}), fresh_db.dsn("kh_migrator", DB))
    out = [c.render() for c in await step.apply(False)]
    assert out[0] == "+ label ember"
    assert sorted(out[1:]) == ["+ job git_push: docs-a", "+ job git_push: docs-b", "+ job git_push: kb-all"]
    assert jobs(fresh_db) == [("docs-a",), ("docs-b",), ("kb-all",)]


async def test_no_new_label_queues_nothing(fresh_db):
    step = LabelsStep(config({"ember": "Ember"}), fresh_db.dsn("kh_migrator", DB))
    await step.apply(False)
    with psycopg.connect(fresh_db.dsn("kh_migrator", DB), autocommit=True) as conn:
        conn.execute("DELETE FROM kh.index_jobs")
    again = LabelsStep(config({"ember": "Changed"}), fresh_db.dsn("kh_migrator", DB))
    assert [c.render() for c in await again.apply(False)] == ["~ label ember"]
    assert jobs(fresh_db) == []
