"""Named principals shared by every leak test."""

from __future__ import annotations

from knowledge_hub.auth.claims import Caller


def _caller(sub: str, audiences: tuple[str, ...], email: str | None, ptype: str = "claude_client") -> Caller:
    return Caller(sub=sub, principal_type=ptype, email=email, audiences=frozenset(audiences), token_id=None)


alice_all = _caller("alice", ("all",), "alice@example.org")
bob_mgmt = _caller("bob", ("all", "mgmt"), "bob@example.org")
carol_hr = _caller("carol", ("all", "hr"), None)
dave_none = _caller("dave", (), "dave@example.org")
eve_nobody = _caller("eve", (), None)
mgmt_only = _caller("mia", ("mgmt",), "mia@example.org")
finance_only = _caller("finn", ("finance",), "finn@example.org")
mgmt_finance = _caller("max", ("finance", "mgmt"), None)
wf_token = _caller("n8n-token:t1", ("all",), None, ptype="n8n_workflow")

PRINCIPALS = {
    "alice_all": alice_all,
    "bob_mgmt": bob_mgmt,
    "carol_hr": carol_hr,
    "dave_none": dave_none,
    "eve_nobody": eve_nobody,
    "mgmt_only": mgmt_only,
    "finance_only": finance_only,
    "mgmt_finance": mgmt_finance,
    "wf_token": wf_token,
}
