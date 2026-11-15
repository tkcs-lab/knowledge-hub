"""propose_change: routing, path rules, dedupe, daily limit, retry and the candidate grants (plan 3.8)."""

import uuid

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import app_conn, insert, uid
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.integration.test_derived_visibility_sql import raw as make_raw
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, carol_hr, dave_none

REPOS = ("canonical-all", "canonical-mgmt", "canonical-hr", "canonical-contracts")


@pytest.fixture
def forgejo():
    return ForgejoFake(
        {r: FakeRepo(commits=[FakeCommit("c1", files={"product/old.md": "old"})]) for r in REPOS}
    )


@pytest.fixture
def indexer_forgejo():
    """The read-only indexer client: any write through it is refused and recorded."""
    return ForgejoFake(
        {r: FakeRepo(commits=[FakeCommit("c1", files={"product/old.md": "old"})]) for r in REPOS},
        read_only=True,
    )


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, forgejo, indexer_forgejo):
    # ``forgejo`` is the bot client (the one that writes); ``deps.forgejo`` is the read-only indexer one.
    return Deps(
        settings=app_settings,
        config=Config(),
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        forgejo=indexer_forgejo,
        forgejo_bot=forgejo,
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def args(sources, **over):
    base = {
        "domain": "product",
        "title": "A change",
        "content": "# Heading\n\nBody text.\n",
        "rationale": "because",
        "source_ids": [f"doc:{s}" for s in sources],
        "new_path": "product/new-" + uid()[:8] + ".md",
    }
    base.update(over)
    return base


def ok(res) -> dict:
    assert not res.is_error, res
    return res.data


def err(res, text) -> None:
    assert res.status == 200 and res.is_error and res.text.endswith(text), res


def n_candidates(admin) -> int:
    return admin.execute("SELECT count(*) FROM kh.candidates").fetchone()[0]


# --- routing -----------------------------------------------------------------------------------


async def test_all_sources_go_to_canonical_all_and_mgmt_to_canonical_mgmt(admin, mcp_client, forgejo):
    a, m = make_doc(admin, ("all",)), make_doc(admin, ("mgmt",))
    r1 = ok(await mcp_client.call(bob_mgmt, "propose_change", args([a])))
    r2 = ok(await mcp_client.call(bob_mgmt, "propose_change", args([m])))
    rows = dict(admin.execute("SELECT id::text, target_repo FROM kh.candidates").fetchall())
    assert rows[r1["candidate_id"]] == "canonical-all" and rows[r2["candidate_id"]] == "canonical-mgmt"
    assert [p["number"] for p in forgejo.repos["canonical-mgmt"].pulls] == [1]


async def test_union_with_all_routes_to_the_narrow_repo(admin, mcp_client):
    # {all} and {mgmt} span two audiences: refused.
    a, m = make_doc(admin, ("all",)), make_doc(admin, ("mgmt",))
    err(
        await mcp_client.call(bob_mgmt, "propose_change", args([a, m])),
        "cannot propose: sources span audiences all, mgmt; no canonical repo is restricted to all of them",
    )
    assert n_candidates(admin) == 0


async def test_email_restricted_source_is_refused(admin, mcp_client):
    d = make_doc(admin, ("all",), ["alice@example.org"])
    err(
        await mcp_client.call(alice_all, "propose_change", args([d])),
        "a cited source is restricted to named people; no canonical repo is that narrow",
    )


async def test_explicit_wider_repo_is_refused(admin, mcp_client):
    # A target document in canonical-all with a source only mgmt may read.
    target = uid()
    insert(
        admin,
        "documents",
        dict(
            id=target,
            repo="canonical-all",
            path="product/t.md",
            doc_key=uid(),
            tier="T0",
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c1",
            content_sha="s",
            required_audiences=["all"],
        ),
    )
    m = make_doc(admin, ("mgmt",))
    res = await mcp_client.call(
        bob_mgmt,
        "propose_change",
        {
            "domain": "product",
            "title": "t",
            "content": "x",
            "rationale": "r",
            "source_ids": [f"doc:{m}"],
            "target_id": f"doc:{target}",
        },
    )
    err(
        res,
        "cannot propose to canonical-all: its audience all is wider than or different from "
        "the sources' audience mgmt",
    )


async def test_target_id_replaces_the_documents_path(admin, mcp_client, forgejo):
    target = uid()
    insert(
        admin,
        "documents",
        dict(
            id=target,
            repo="canonical-all",
            path="product/old.md",
            doc_key=uid(),
            tier="T0",
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c1",
            content_sha="s",
            required_audiences=["all"],
        ),
    )
    s = make_doc(admin, ("all",))
    res = ok(
        await mcp_client.call(
            alice_all,
            "propose_change",
            {
                "domain": "product",
                "title": "t",
                "content": "new",
                "rationale": "r",
                "source_ids": [f"doc:{s}"],
                "target_id": f"doc:{target}",
            },
        )
    )
    assert forgejo.files[("canonical-all", res["branch"], "product/old.md")] == "new\n"
    assert admin.execute("SELECT target_doc_id::text, target_path FROM kh.candidates").fetchall() == [
        (target, "product/old.md")
    ]


async def test_target_with_a_governance_path_is_bad_path(admin, mcp_client):
    target = uid()
    insert(
        admin,
        "documents",
        dict(
            id=target,
            repo="canonical-all",
            path="CODEOWNERS",
            doc_key=uid(),
            tier="T0",
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c1",
            content_sha="s",
            required_audiences=["all"],
        ),
    )
    s = make_doc(admin, ("all",))
    res = await mcp_client.call(
        alice_all,
        "propose_change",
        {
            "domain": "product",
            "title": "t",
            "content": "x",
            "rationale": "r",
            "source_ids": [f"doc:{s}"],
            "target_id": f"doc:{target}",
        },
    )
    err(
        res,
        "new_path must be a new markdown file under portfolio/, product/ or technical/ in the "
        "given domain (lowercase letters, digits, - _ /)",
    )


# --- paths -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".forgejo/workflows/x.md",
        "../x.md",
        "CODEOWNERS",
        "product/../x.md",
        "product/old.md",
        "technical/x.md",
        "product//x.md",
        "product/X.md",
        "product/x.txt",
        "/product/x.md",
    ],
)
async def test_bad_paths_are_refused(admin, mcp_client, forgejo, path):
    s = make_doc(admin, ("all",))
    res = await mcp_client.call(alice_all, "propose_change", args([s], new_path=path))
    err(res, "in the given domain (lowercase letters, digits, - _ /)")
    assert n_candidates(admin) == 0 and forgejo.written_refs == []


async def test_exactly_one_of_target_and_path(admin, mcp_client):
    s = make_doc(admin, ("all",))
    base = args([s])
    del base["new_path"]
    err(
        await mcp_client.call(alice_all, "propose_change", base), "give exactly one of target_id and new_path"
    )
    err(
        await mcp_client.call(
            alice_all, "propose_change", {**base, "new_path": "product/a.md", "target_id": f"doc:{s}"}
        ),
        "give exactly one of target_id and new_path",
    )


@pytest.mark.parametrize("n", [0, 51])
async def test_source_count_bounds(admin, mcp_client, n):
    res = await mcp_client.call(
        alice_all, "propose_change", args([make_doc(admin, ("all",)) for _ in range(n)])
    )
    assert res.is_error
    assert n_candidates(admin) == 0


async def test_hidden_source_reads_like_a_missing_one(admin, mcp_client):
    hidden = make_doc(admin, ("mgmt",))
    r = make_raw(admin, ("hr",))
    a = await mcp_client.call(alice_all, "propose_change", args([hidden]))
    b = await mcp_client.call(alice_all, "propose_change", args([r]) | {"source_ids": [f"raw:{r}"]})
    c = await mcp_client.call(alice_all, "propose_change", args([uid()]))
    err(a, "not found")
    assert (a.text, a.is_error) == (b.text, b.is_error) == (c.text, c.is_error)
    assert hidden not in a.text and n_candidates(admin) == 0


# --- dedupe, limit -----------------------------------------------------------------------------


async def test_second_identical_call_is_a_duplicate(admin, mcp_client):
    s = make_doc(admin, ("all",))
    a = args([s], new_path="product/dup.md")
    first = ok(await mcp_client.call(alice_all, "propose_change", a))
    # Whitespace noise does not make a different proposal.
    res = await mcp_client.call(
        alice_all, "propose_change", a | {"content": "\r\n# Heading  \r\n\r\nBody text.\r\n\r\n"}
    )
    err(res, f"an identical proposal is already open: {first['candidate_id']} (PR #{first['pr_number']})")
    assert n_candidates(admin) == 1


async def test_sixth_call_in_a_domain_and_repo_is_limited(admin, mcp_client, forgejo):
    s = make_doc(admin, ("all",))
    for i in range(5):
        ok(await mcp_client.call(alice_all, "propose_change", args([s], content=f"c{i}")))
    res = await mcp_client.call(alice_all, "propose_change", args([s], content="c5"))
    assert (
        res.is_error
        and "daily promotion limit for domain product in canonical-all reached (5); try again after "
        in res.text
    )
    assert res.text.endswith("00:00 UTC")
    assert n_candidates(admin) == 5
    # Another domain is counted apart.
    ok(
        await mcp_client.call(
            alice_all, "propose_change", args([s], domain="technical", new_path="technical/z.md")
        )
    )


# --- the pull request --------------------------------------------------------------------------


async def test_pr_carries_labels_branch_template_and_main_is_untouched(admin, mcp_client, forgejo):
    d, raw_id = make_doc(admin, ("all",)), make_raw(admin, ("all",))
    res = ok(
        await mcp_client.call(
            alice_all,
            "propose_change",
            args([d, raw_id])
            | {
                "source_ids": [f"doc:{d}", f"raw:{raw_id}"],
                "new_path": "product/n.md",
                "rationale": "why not",
            },
        )
    )
    cid = res["candidate_id"]
    (pull,) = forgejo.opened
    assert res["branch"] == f"bot/promote/{cid}" == pull["head"]["ref"]
    assert pull["base"] == {"ref": "main"}
    assert {lb["name"] for lb in pull["labels"]} == {"promotion", "domain/product"}
    for heading in ("## What", "## Why", "## Sources", "## Candidate"):
        assert heading in pull["body"]
    assert f"- doc:{d}" in pull["body"] and f"- raw:{raw_id}" in pull["body"] and cid in pull["body"]
    assert "why not" in pull["body"]
    assert set(forgejo.written_refs) == {f"bot/promote/{cid}"}
    assert not any(ref == "main" for ref in forgejo.written_refs)
    assert (
        forgejo.files[("canonical-all", f"bot/promote/{cid}", "product/n.md")] == "# Heading\n\nBody text.\n"
    )
    assert admin.execute(
        "SELECT state, branch, pr_number, pr_url, error, derived_id, required_audiences, allowed_emails,"
        " created_by_sub FROM kh.candidates"
    ).fetchall() == [
        (
            "pr_open",
            f"bot/promote/{cid}",
            1,
            "https://forgejo.test/kh/canonical-all/pulls/1",
            None,
            None,
            ["all"],
            None,
            "alice",
        )
    ]
    assert admin.execute("SELECT count(*) FROM kh.candidate_sources").fetchone() == (2,)


async def test_single_derived_source_sets_derived_id(admin, mcp_client):
    from tests.integration.test_derived_visibility_sql import bare_derived

    x = bare_derived(admin, ("all",))
    res = ok(await mcp_client.call(alice_all, "propose_change", args([x]) | {"source_ids": [f"derived:{x}"]}))
    assert admin.execute(
        "SELECT derived_id::text FROM kh.candidates WHERE id = %s", (res["candidate_id"],)
    ).fetchone() == (x,)


async def test_visibility_stores_the_full_union(admin, mcp_client):
    # mgmt-only sources with the all audience: a source needing {all, mgmt} routes by union {all, mgmt}
    # and is refused as span; a pure {mgmt} source is stored as {mgmt}.
    m = make_doc(admin, ("mgmt",))
    res = ok(await mcp_client.call(bob_mgmt, "propose_change", args([m])))
    assert admin.execute(
        "SELECT required_audiences FROM kh.candidates WHERE id = %s", (res["candidate_id"],)
    ).fetchone() == (["mgmt"],)


async def test_no_forgejo_connection_is_a_fixed_text(admin, mcp_client, deps):
    deps.forgejo = None
    deps.forgejo_bot = None
    s = make_doc(admin, ("all",))
    err(
        await mcp_client.call(alice_all, "propose_change", args([s])),
        "promotion is not available: no Forgejo connection is configured",
    )
    assert n_candidates(admin) == 0


async def test_promotion_is_refused_without_the_bot_client(admin, mcp_client, deps, indexer_forgejo):
    deps.forgejo_bot = None  # the indexer client is still there but must not stand in
    s = make_doc(admin, ("all",))
    err(
        await mcp_client.call(alice_all, "propose_change", args([s])),
        "promotion is not available: no Forgejo connection is configured",
    )
    assert n_candidates(admin) == 0
    assert indexer_forgejo.refused_writes == [] and indexer_forgejo.opened == []


async def test_writes_go_through_the_bot_client_never_the_indexer_one(
    admin, mcp_client, forgejo, indexer_forgejo
):
    s = make_doc(admin, ("all",))
    ok(await mcp_client.call(alice_all, "propose_change", args([s])))
    assert [c for c in forgejo.calls if c in ("create_branch", "put_file", "open_pull")] == [
        "create_branch",
        "put_file",
        "open_pull",
    ]
    assert indexer_forgejo.refused_writes == []
    assert not {"create_branch", "put_file", "open_pull", "path_exists"} & set(indexer_forgejo.calls)


# --- failure and retry -------------------------------------------------------------------------


async def test_failed_forgejo_call_leaves_a_failed_candidate_that_a_retry_completes(
    admin, mcp_client, forgejo
):
    s = make_doc(admin, ("all",))
    a = args([s], new_path="product/retry.md")
    forgejo.labels["canonical-all"] = {"promotion"}  # domain/product is missing: open_pull fails
    res = await mcp_client.call(alice_all, "propose_change", a)
    assert (
        res.is_error
        and "could not open the pull request; call propose_change again with candidate_id=" in res.text
    )
    ((cid, state, error),) = admin.execute("SELECT id::text, state, error FROM kh.candidates").fetchall()
    assert state == "failed" and error and f"candidate_id={cid}" in res.text
    assert "label" not in res.text and forgejo.opened == []

    forgejo.labels["canonical-all"] = {"promotion", "domain/product"}
    done = ok(await mcp_client.call(alice_all, "propose_change", a | {"candidate_id": cid}))
    assert done["candidate_id"] == cid and done["branch"] == f"bot/promote/{cid}"
    assert admin.execute("SELECT state, pr_number, error FROM kh.candidates").fetchall() == [
        ("pr_open", 1, None)
    ]
    assert n_candidates(admin) == 1 and len(forgejo.opened) == 1


async def test_retry_by_another_sub_is_bad_candidate(admin, mcp_client, forgejo):
    s = make_doc(admin, ("all",))
    a = args([s])
    forgejo.labels["canonical-all"] = set()
    await mcp_client.call(alice_all, "propose_change", a)
    (cid,) = admin.execute("SELECT id::text FROM kh.candidates").fetchone()
    forgejo.labels["canonical-all"] = {"promotion", "domain/product"}
    bad = "candidate_id must name one of your own failed proposals"
    err(await mcp_client.call(bob_mgmt, "propose_change", a | {"candidate_id": cid}), bad)
    err(await mcp_client.call(alice_all, "propose_change", a | {"candidate_id": "nope"}), bad)
    err(await mcp_client.call(alice_all, "propose_change", a | {"candidate_id": str(uuid.uuid4())}), bad)
    # A retry may not change what is proposed.
    err(
        await mcp_client.call(alice_all, "propose_change", a | {"candidate_id": cid, "content": "other"}), bad
    )
    assert forgejo.opened == []


async def test_retry_of_an_open_candidate_is_bad_candidate(admin, mcp_client):
    s = make_doc(admin, ("all",))
    a = args([s])
    first = ok(await mcp_client.call(alice_all, "propose_change", a))
    err(
        await mcp_client.call(alice_all, "propose_change", a | {"candidate_id": first["candidate_id"]}),
        "candidate_id must name one of your own failed proposals",
    )


# --- the candidate tables ----------------------------------------------------------------------


async def test_a_caller_cannot_see_a_candidate_whose_sources_it_cannot_see(admin, mcp_client, fresh_db):
    m = make_doc(admin, ("mgmt",))
    res = ok(await mcp_client.call(bob_mgmt, "propose_change", args([m])))
    cid = res["candidate_id"]
    for who, sees in ((bob_mgmt, 1), (alice_all, 0), (carol_hr, 0), (dave_none, 0)):
        with app_conn(fresh_db, who) as conn:
            assert conn.execute("SELECT count(*) FROM kh.candidates WHERE id = %s", (cid,)).fetchone() == (
                sees,
            )
            assert conn.execute(
                "SELECT count(*) FROM kh.candidate_sources WHERE candidate_id = %s", (cid,)
            ).fetchone() == (sees,)


async def test_kh_app_changes_a_candidate_only_through_candidate_mark(admin, mcp_client, fresh_db):
    s = make_doc(admin, ("all",))
    cid = ok(await mcp_client.call(alice_all, "propose_change", args([s])))["candidate_id"]
    with app_conn(fresh_db, alice_all) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE kh.candidates SET state = 'promoted' WHERE id = %s", (cid,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM kh.candidates WHERE id = %s", (cid,))
        # Only along the allowed edges, only for the owner.
        assert conn.execute(
            "SELECT kh.candidate_mark(%s, 'promoted', null, null, null, null)", (cid,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT kh.candidate_mark(%s, 'failed', null, null, null, 'x')", (cid,)
        ).fetchone() == (False,)
    with app_conn(fresh_db, bob_mgmt) as conn:
        assert conn.execute(
            "SELECT kh.candidate_mark(%s, 'failed', null, null, null, 'x')", (cid,)
        ).fetchone() == (False,)
    assert admin.execute("SELECT state FROM kh.candidates").fetchone() == ("pr_open",)
