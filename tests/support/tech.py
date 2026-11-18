"""Group config plus a tech repo, shared by the tech document tests."""

from __future__ import annotations

from knowledge_hub.config import parse_config
from tests.support.principals import KH_CONFIG

TECH_REPO = "ember-core"

_base = KH_CONFIG.model_dump()

TECH_CONFIG = parse_config(
    {
        "people": _base["people"],
        "groups": _base["groups"],
        "labels": {"ember": "Ember product"},
        "forgejo_web_url": "https://forgejo.test.example",
        "tech_org": "kh-tech",
        "tech_repos": {TECH_REPO: {"labels": ["ember"]}},
    }
)
