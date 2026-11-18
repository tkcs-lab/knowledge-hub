"""Pure edits of the loaded ``kh.yaml`` document (plan 45, 3.5).

Every function edits ``doc.data`` in place, returns ``True`` when something changed and
raises ``OpError`` for a refusal. The config validation reports everything else.
"""

from __future__ import annotations

from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from knowledge_hub.admin.yamledit import Doc


class OpError(ValueError):
    pass


def _flow(items: list[str]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _map(doc: Doc, key: str) -> Any:
    value = doc.data.get(key)
    if value is None:
        value = CommentedMap()
        doc.data[key] = value
    return value


def _list(doc: Doc, key: str) -> Any:
    value = doc.data.get(key)
    if value is None:
        value = _flow([])
        doc.data[key] = value
    return value


def _group(doc: Doc, name: str) -> Any:
    groups = doc.data.get("groups") or {}
    if name not in groups:
        raise OpError(f"unknown group {name}")
    group = groups[name]
    if group is None:
        group = CommentedMap()
        groups[name] = group
    return group


def _members(group: Any, key: str) -> Any:
    value = group.get(key)
    if value is None:
        value = _flow([])
        group[key] = value
    return value


def _index(items: Any, login: str) -> int | None:
    want = login.casefold()
    for i, item in enumerate(items):
        if str(item).casefold() == want:
            return i
    return None


def _stored(doc: Doc, login: str) -> str | None:
    want = login.casefold()
    for key in doc.data.get("people") or {}:
        if str(key).casefold() == want:
            return str(key)
    return None


def _check_label(doc: Doc, name: str) -> None:
    if name not in (doc.data.get("labels") or {}):
        raise OpError(f"unknown label {name}")


# labels


def label_add(doc: Doc, name: str, description: str) -> bool:
    labels = _map(doc, "labels")
    if name in labels:
        raise OpError(f"label {name} already exists")
    labels[name] = description
    return True


def label_describe(doc: Doc, name: str, description: str) -> bool:
    _check_label(doc, name)
    labels = doc.data["labels"]
    if labels[name] == description:
        return False
    labels[name] = description
    return True


def label_remove(doc: Doc, name: str) -> bool:
    _check_label(doc, name)
    for repo, tech in (doc.data.get("tech_repos") or {}).items():
        if name in ((tech or {}).get("labels") or []):
            raise OpError(f"label {name} is used by tech repo {repo}; remove it from tech_repos first")
    del doc.data["labels"][name]
    archived = doc.data.get("archived_labels")
    if archived is not None and name in archived:
        archived.remove(name)
    return True


def label_archive(doc: Doc, name: str) -> bool:
    _check_label(doc, name)
    archived = doc.data.get("archived_labels")
    if archived is not None and name in archived:
        return False
    _list(doc, "archived_labels").append(name)
    return True


def label_unarchive(doc: Doc, name: str) -> bool:
    _check_label(doc, name)
    archived = doc.data.get("archived_labels")
    if archived is None or name not in archived:
        return False
    archived.remove(name)
    return True


# groups


def group_add(
    doc: Doc,
    name: str,
    members: list[str],
    approvers: list[str],
    repo: str | None,
    slack_channel: str | None,
) -> bool:
    groups = _map(doc, "groups")
    if name in groups:
        raise OpError(f"group {name} already exists")
    group = CommentedMap()
    if repo is not None:
        group["repo"] = repo
    group["members"] = _flow([_stored(doc, m) or m for m in members])
    group["approvers"] = _flow([_stored(doc, a) or a for a in approvers])
    if slack_channel is not None:
        group["slack_channel"] = slack_channel
    groups[name] = group
    return True


def group_remove(doc: Doc, name: str) -> bool:
    _group(doc, name)
    del doc.data["groups"][name]
    return True


def group_set_archived(doc: Doc, name: str, flag: bool) -> bool:
    group = _group(doc, name)
    if flag:
        if group.get("archived") is True:
            return False
        group["archived"] = True
        return True
    if "archived" not in group:
        return False
    del group["archived"]
    return True


def member_add(doc: Doc, group: str, login: str) -> bool:
    g = _group(doc, group)
    stored = _stored(doc, login)
    if stored is None:
        raise OpError(f"login {login} is not in people; run kh person add first")
    members = _members(g, "members")
    if _index(members, stored) is not None:
        return False
    members.append(stored)
    return True


def _approver_remove(g: Any, group: str, login: str) -> None:
    approvers = _members(g, "approvers")
    i = _index(approvers, login)
    if i is None:
        raise OpError(f"{login} is not an approver of {group}")
    if len(approvers) <= 1:
        raise OpError(f"group {group} needs at least one approver")
    del approvers[i]


def member_remove(doc: Doc, group: str, login: str, with_approver: bool) -> bool:
    g = _group(doc, group)
    members = _members(g, "members")
    i = _index(members, login)
    if i is None:
        raise OpError(f"{login} is not a member of {group}")
    approvers = _members(g, "approvers")
    if _index(approvers, login) is not None:
        if not with_approver:
            raise OpError(
                f"{login} is an approver of {group}; remove the approver first or pass --with-approver"
            )
        _approver_remove(g, group, login)
    del members[_index(members, login)]  # type: ignore[arg-type]
    return True


def approver_add(doc: Doc, group: str, login: str) -> bool:
    g = _group(doc, group)
    members = _members(g, "members")
    i = _index(members, login)
    if i is None:
        raise OpError(f"{login} is not a member of {group}")
    approvers = _members(g, "approvers")
    if _index(approvers, login) is not None:
        return False
    approvers.append(members[i])
    return True


def approver_remove(doc: Doc, group: str, login: str) -> bool:
    _approver_remove(_group(doc, group), group, login)
    return True


# people


def person_add(doc: Doc, login: str, email: str) -> bool:
    if _stored(doc, login) is not None:
        raise OpError(f"person {login} already exists")
    _map(doc, "people")[login] = email.lower()
    return True


def person_remove(doc: Doc, login: str, from_groups: bool) -> bool:
    stored = _stored(doc, login)
    if stored is None:
        raise OpError(f"unknown person {login}")
    groups = doc.data.get("groups") or {}
    member_of: list[str] = []
    for name, g in groups.items():
        g = g or {}
        if _index(g.get("members") or [], login) is not None:
            member_of.append(name)
        approvers = g.get("approvers") or []
        if from_groups and _index(approvers, login) is not None and len(approvers) <= 1:
            raise OpError(f"person {stored} is the last approver of {name}; add another approver first")
    if member_of and not from_groups:
        raise OpError(
            f"person {stored} is a member of {', '.join(member_of)}; "
            "pass --from-groups to remove them from every group"
        )
    for g in groups.values():
        for key in ("members", "approvers"):
            items = (g or {}).get(key) or []
            i = _index(items, login)
            if i is not None:
                del items[i]
    del doc.data["people"][stored]
    return True
