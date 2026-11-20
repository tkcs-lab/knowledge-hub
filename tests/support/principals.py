"""Named principals and the group config every leak test shares."""

from __future__ import annotations

from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import GroupConfig, KhConfig

KH_CONFIG = KhConfig(
    people={
        "alice": "alice@example.test",
        "bob": "bob@example.test",
        "carol": "carol@example.test",
        "multi": "multi@example.test",
        "dave": "dave@example.test",
    },
    groups={
        "all": GroupConfig(members=["alice", "multi"], approvers=["alice"]),
        "mgmt": GroupConfig(members=["bob", "multi"], approvers=["bob"]),
        "hr": GroupConfig(members=["carol"], approvers=["carol"]),
    },
)


def _person(login: str) -> Caller:
    email = KH_CONFIG.people[login]
    audiences = KH_CONFIG.audiences_for_email(email)
    return Caller(sub=login, principal_type="claude_client", email=email, audiences=audiences, token_id=None)


alice_all = _person("alice")
bob_mgmt = _person("bob")
carol_hr = _person("carol")
dave_none = _person("dave")
multi = _person("multi")
wf_token = Caller(
    sub="n8n-token:t1",
    principal_type="n8n_workflow",
    email=None,
    audiences=frozenset({"all"}),
    token_id=None,
)


def _workflow(name: str, *groups: str) -> Caller:
    return Caller(
        sub=f"key:{name}",
        principal_type="n8n_workflow",
        email=None,
        audiences=frozenset(groups),
        token_id=None,
    )


wf_all = _workflow("wf-all", "all")
wf_mgmt = _workflow("wf-mgmt", "mgmt")
wf_hr = _workflow("wf-hr", "hr")
wf_none = _workflow("wf-none")


def _legacy(sub: str, audiences: tuple[str, ...], email: str | None, ptype: str = "claude_client") -> Caller:
    return Caller(sub=sub, principal_type=ptype, email=email, audiences=frozenset(audiences), token_id=None)


# Hand-listed callers of the old audience model, kept only so tests/integration/test_rls_tables.py
# and its seed (rewritten in p2) keep passing until then. Do not use them in new tests.
PRINCIPALS = {
    "alice_all": _legacy("alice", ("all",), "alice@example.org"),
    "bob_mgmt": _legacy("bob", ("all", "mgmt"), "bob@example.org"),
    "carol_hr": _legacy("carol", ("all", "hr"), None),
    "dave_none": _legacy("dave", (), "dave@example.org"),
    "eve_nobody": _legacy("eve", (), None),
    "mgmt_only": _legacy("mia", ("mgmt",), "mia@example.org"),
    "finance_only": _legacy("finn", ("finance",), "finn@example.org"),
    "mgmt_finance": _legacy("max", ("finance", "mgmt"), None),
    "wf_token": _legacy("n8n-token:t1", ("all",), None, ptype="n8n_workflow"),
}
