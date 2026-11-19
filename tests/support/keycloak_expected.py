"""What the Keycloak realm file yields, derived from the file (plan 58, 3.5).

Used by the unit test (through a MockTransport) and by the live test (against a real Keycloak).
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.directory.model import DirectoryUser, GroupFetch
from knowledge_hub.directory.provider import GroupRefs

REALM_FILE = Path(__file__).with_name("keycloak_realm.json")
DOMAINS = ["example.org"]
REFS = {"g": GroupRefs(("kh-all",), ("kh-all-approvers",))}


def load_realm() -> dict:
    return json.loads(REALM_FILE.read_text(encoding="utf-8"))


def realm_users() -> list[dict]:
    return [u for u in load_realm()["users"] if "serviceAccountClientId" not in u]


def users_of(path: str) -> list[dict]:
    """The realm's users that are direct members of the group at `path`."""
    return [u for u in realm_users() if path in u.get("groups", [])]


def directory_user(row: dict) -> DirectoryUser:
    return DirectoryUser(
        email=row["email"],
        enabled=row["enabled"],
        email_verified=row["emailVerified"],
        username=row["username"],
    )


def expected_fetch() -> dict[str, GroupFetch]:
    return {
        "g": GroupFetch(
            tuple(directory_user(u) for u in users_of("/kh-all")),
            tuple(directory_user(u) for u in users_of("/kh-all-approvers")),
        )
    }


def config() -> KhConfig:
    return KhConfig(
        people={"root": "root@example.org"},
        groups={"g": GroupConfig(members=["root"], approvers=["root"])},
    )


# Expected through `rules.effective` with DOMAINS and the static member and approver `root`:
# dave is disabled, erin is unverified, frank has a foreign domain, gina is only in the child
# group (not a direct member), carol approves without being a member.
EXPECTED_MEMBERS = frozenset({"root", "alice", "bob"})
EXPECTED_APPROVERS = frozenset({"root", "alice"})
EXPECTED_SKIPS = frozenset(
    {
        ("member", "dave@example.org", "disabled"),
        ("member", "erin@example.org", "email not verified"),
        ("member", "frank@other.com", "domain not allowed"),
        ("approver", "carol@example.org", "approver is not a member"),
    }
)
