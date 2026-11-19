import sys
import types

import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.directory import provider as provider_mod
from knowledge_hub.directory.provider import GroupRefs, ProviderError, build_provider, refs_from_config

pytestmark = pytest.mark.unit


def test_build_provider_refuses_unknown_name():
    with pytest.raises(ValueError, match="directory: unknown provider ldap"):
        build_provider("ldap", object())  # type: ignore[arg-type]


def test_build_provider_calls_make_of_the_module(monkeypatch):
    seen = {}
    fake = types.ModuleType("fake_directory_mod")
    fake.make = lambda settings, http: seen.update(settings=settings, http=http) or "p"
    monkeypatch.setitem(sys.modules, "fake_directory_mod", fake)
    monkeypatch.setitem(provider_mod.PROVIDERS, "google", "fake_directory_mod")
    assert build_provider("google", "S", http="H") == "p"  # type: ignore[arg-type]
    assert seen == {"settings": "S", "http": "H"}


def test_provider_table():
    assert provider_mod.PROVIDERS == {
        "google": "knowledge_hub.directory.google",
        "keycloak": "knowledge_hub.directory.keycloak",
    }
    assert issubclass(ProviderError, Exception)


def test_refs_from_config_skips_archived_and_empty_groups():
    c = KhConfig(
        people={"sam": "sam@x.org"},
        groups={
            "a": GroupConfig(
                members=["sam"],
                approvers=["sam"],
                directory={"google": {"members": ["a@x.org"], "approvers": ["b@x.org"]}},
            ),
            "b": GroupConfig(
                members=["sam"],
                approvers=["sam"],
                archived=True,
                directory={"google": {"members": ["c@x.org"]}},
            ),
            "c": GroupConfig(members=["sam"], approvers=["sam"], directory={"google": {}}),
            "d": GroupConfig(members=["sam"], approvers=["sam"], directory={"keycloak": {"members": ["/d"]}}),
            "e": GroupConfig(members=["sam"], approvers=["sam"]),
        },
    )
    assert refs_from_config(c, "google") == {"a": GroupRefs(("a@x.org",), ("b@x.org",))}
    assert refs_from_config(c, "keycloak") == {"d": GroupRefs(("/d",), ())}
