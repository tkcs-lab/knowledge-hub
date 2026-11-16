from pathlib import Path

import pytest
import yaml

from knowledge_hub import cli
from knowledge_hub.config import Config
from knowledge_hub.forgejo import ForgejoClient, ForgejoError
from knowledge_hub.governance.ci import CiError, build_id_index, run_validate
from knowledge_hub.governance.owners import build_owners, dump_owners
from knowledge_hub.grist.client import GristClient
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document
from tests.support.forgejo_http import FakeForgejoHttp, grist_transport

pytestmark = pytest.mark.unit

DATA = Path(__file__).parent.parent / "data" / "validate"
CONFIG = Config()
OWNERS_YML = dump_owners(
    build_owners(
        "all",
        portfolio_team="owners-portfolio",
        product_team="owners-product",
        technical_team="owners-technical",
        owner_team=None,
        admin_team="kh-admins",
    )
)


def doc(ident, *, entities=(), domain="product", typ="requirement", owner="owners-product"):
    ents = f"entities: [{', '.join(entities)}]\n" if entities else ""
    return (
        f"---\nid: {ident}\ntitle: T\ndomain: {domain}\nreview_by: 2030-01-01\n"
        f"type: {typ}\nowner: {owner}\n{ents}---\nBody.\n"
    )


def path_of(ident):
    return f"product/alpha/requirements/{ident}.md"


def repo(tree, compare=None, sha="s1"):
    return {"sha": sha, "tree": tree, "compare": compare or []}


def world(own_tree, changed, others=None):
    tree = {"owners.yml": OWNERS_YML, **own_tree}
    out = {
        "canonical-all": repo(tree, [{"filename": p, "status": "added"} for p in changed]),
        "canonical-mgmt": repo({}),
        "canonical-hr": repo({}),
        "canonical-contracts": repo({}),
    }
    out.update(others or {})
    return out


def clients(fake, tables, status_fake=None):
    read = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    status = ForgejoClient("http://f", "t", "kh", transport=(status_fake or fake).transport)
    grist = GristClient("http://g", "doc", "k", transport=grist_transport(tables))
    return read, status, grist


TABLES = {"Products": ["prd:alpha"], "Components": ["cmp:api"], "Customers": ["cus:acme"]}


async def run(fake, tables=TABLES, *, post=True, status_fake=None):
    read, status, grist = clients(fake, tables, status_fake)
    return await run_validate(read, status, grist, CONFIG, "canonical-all", "base", "head", post=post)


async def test_clean_pr_passes_and_posts_success():
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]))
    result = await run(fake)
    assert result.ok and result.lines == []
    assert fake.statuses == [
        {"context": "kh/validate", "state": "success", "description": "all checks passed"}
    ]
    assert fake.status_targets == [("canonical-all", "head")]


async def test_unknown_entity_fails():
    p = path_of("bad")
    fake = FakeForgejoHttp(world({p: doc("bad", entities=["prd:ghost"])}, [p]))
    result = await run(fake)
    assert not result.ok
    assert result.lines == [f"{p}: entity not found: prd:ghost"]
    assert fake.statuses[0]["state"] == "failure"
    assert fake.statuses[0]["description"] == "1 problems"


async def test_forgejo_500_posts_error_and_raises():
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]), fail={"/compare/": 500})
    with pytest.raises(ForgejoError):
        await run(fake)
    assert fake.statuses == [
        {"context": "kh/validate", "state": "error", "description": "check could not run"}
    ]


async def test_no_status_posts_nothing():
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]))
    await run(fake, post=False)
    assert fake.statuses == []


async def test_status_goes_to_status_client():
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]))
    status_fake = FakeForgejoHttp(world({}, []))
    await run(fake, status_fake=status_fake)
    assert fake.statuses == []
    assert [s["state"] for s in status_fake.statuses] == ["success"]


async def test_missing_owners_is_ci_error():
    w = world({}, [])
    del w["canonical-all"]["tree"]["owners.yml"]
    fake = FakeForgejoHttp(w)
    with pytest.raises(CiError):
        await run(fake)
    assert fake.statuses[0]["state"] == "error"


async def test_too_many_files():
    paths = [path_of(f"d{i}") for i in range(201)]
    fake = FakeForgejoHttp(world({p: doc("x") for p in paths}, paths))
    result = await run(fake)
    assert not result.ok
    assert result.lines == [f"{paths[0]}: more than 200 markdown files changed"]
    assert not fake.paths("/raw/d")


async def test_too_large_file():
    p = path_of("big")
    big = doc("big") + "x" * 1048576
    fake = FakeForgejoHttp(world({p: big}, [p]))
    result = await run(fake)
    assert result.lines == [f"{p}: file is larger than 1 MiB"]


async def test_skips_removed_dot_and_non_markdown():
    changed = [
        {"filename": path_of("gone"), "status": "removed"},
        {"filename": ".forgejo/pull_request_template.md", "status": "added"},
        {"filename": "schema/x.json", "status": "added"},
    ]
    w = world({}, [])
    w["canonical-all"]["compare"] = changed
    fake = FakeForgejoHttp(w)
    result = await run(fake)
    assert result.ok
    assert fake.paths("/raw/") == ["/api/v1/repos/kh/canonical-all/raw/owners.yml"]


async def test_request_budget():
    ids = [f"d{i}" for i in range(20)]
    paths = [path_of(i) for i in ids]
    fake = FakeForgejoHttp(world({p: doc(i) for i, p in zip(ids, paths, strict=True)}, paths))
    read, status, grist = clients(fake, TABLES)
    result = await run_validate(read, status, grist, CONFIG, "canonical-all", "base", "head")
    assert result.ok
    posts = [r for r in fake.requests if r.method == "POST"]
    gets = [r for r in fake.requests if r.method == "GET"]
    assert len(posts) == 1
    assert len([r for r in gets if "/compare/" in r.url.path]) == 1
    assert len([r for r in gets if "/raw/" in r.url.path]) == 21  # 20 files + owners.yml
    assert len([r for r in gets if "/branches/" in r.url.path or "/git/trees/" in r.url.path]) <= 8
    assert len(gets) <= 1 + 21 + 8


async def test_id_index_never_reads_other_repos_files():
    w = world({path_of("mine"): doc("mine")}, [])
    w["canonical-hr"] = repo({"product/alpha/requirements/theirs.md": doc("theirs")})
    fake = FakeForgejoHttp(w)
    read, _, _ = clients(fake, {})
    own, elsewhere = await build_id_index(read, CONFIG, "canonical-all", "head")
    assert own == {"mine": [path_of("mine")]}
    assert elsewhere == {"theirs"}
    assert not fake.paths("/raw/")


async def test_id_index_skips_missing_branch_and_404():
    w = world({}, [])
    w["canonical-hr"] = repo({}, sha=None)
    fake = FakeForgejoHttp(w, fail={"canonical-mgmt/git/trees": 404})
    read, _, _ = clients(fake, {})
    assert await build_id_index(read, CONFIG, "canonical-all", "head") == ({}, set())


@pytest.mark.parametrize(
    "fail",
    [{"canonical-hr/git/trees": 403}, {"canonical-hr/git/trees": 404}, {"canonical-hr/branches": 403}],
)
async def test_id_index_skips_repo_the_token_cannot_see(fail):
    fake = FakeForgejoHttp(world({}, [], {"canonical-hr": repo({"x.md": "x"})}), fail=fail)
    read, _, _ = clients(fake, {})
    assert await build_id_index(read, CONFIG, "canonical-all", "head") == ({}, set())


async def test_clean_pr_passes_when_other_repos_are_hidden():
    p = path_of("good")
    fail = {"canonical-mgmt/": 403, "canonical-hr/": 403, "canonical-contracts/": 403}
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]), fail=fail)
    result = await run(fake)
    assert result.ok and result.lines == []
    assert [s["state"] for s in fake.statuses] == ["success"]


async def test_unreadable_other_repo_error_names_nothing():
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]), fail={"canonical-hr/branches": 500})
    with pytest.raises(CiError) as info:
        await run(fake)
    assert str(info.value) == "another repository could not be read"
    assert info.value.__cause__ is None
    assert [s["state"] for s in fake.statuses] == ["error"]


async def test_cus_without_customers_table_is_not_allowed_and_not_read():
    p = path_of("cites")
    fake = FakeForgejoHttp(world({p: doc("cites", entities=["cus:acme"])}, [p]))
    t = grist_transport({"Products": ["prd:alpha"], "Components": ["cmp:api"]})
    grist = GristClient("http://g", "doc", "k", transport=t)
    read = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    status = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    result = await run_validate(read, status, grist, CONFIG, "canonical-all", "base", "head")
    assert result.lines == [f"{p}: entity cus:acme is not allowed in this repository"]
    assert not any("Customers" in r.url.path for r in t.requests)


async def test_cus_is_read_and_accepted_where_the_audience_may_cite_it():
    p = path_of("cites")
    w = {
        "canonical-contracts": repo(
            {"owners.yml": OWNERS_YML, p: doc("cites", entities=["cus:acme", "prd:alpha"])},
            [{"filename": p, "status": "added"}],
        ),
        "canonical-all": repo({}),
        "canonical-mgmt": repo({}),
        "canonical-hr": repo({}),
    }
    fake = FakeForgejoHttp(w)
    t = grist_transport(TABLES)
    grist = GristClient("http://g", "doc", "k", transport=t)
    read = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    status = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    result = await run_validate(read, status, grist, CONFIG, "canonical-contracts", "base", "head")
    assert result.lines == [] and result.ok
    assert any("Customers" in r.url.path for r in t.requests)


@pytest.mark.parametrize("collide_in", ["other", "same"])
async def test_no_leak(collide_in):
    p = "technical/gateway/api/adr/0001-use-grpc.md"
    other_path = "technical/secretprod/api/adr/0001-hr-secret-slug.md"
    adr = doc("adr-api-0001", entities=["cus:acme"], domain="technical", typ="adr", owner="owners-technical")
    own = {p: adr}
    others = {}
    if collide_in == "other":
        others["canonical-hr"] = repo({other_path: adr})
    else:
        own["technical/gateway/api/adr/0001-hr-secret-slug.md"] = adr
    fake = FakeForgejoHttp(world(own, [p], others))
    tables = {
        "Products": ["prd:gateway"],
        "Components": ["cmp:api"],
        "Customers": [{"key": "cus:acme", "name": "acme corp"}],
    }
    result = await run(fake, tables)
    assert result.lines == [
        f"{p}: entity cus:acme is not allowed in this repository",
        f"{p}: id already in use",
    ]
    text_out = "\n".join(result.lines)
    for secret in ("canonical-hr", "secretprod", "hr-secret-slug", "acme corp"):
        assert secret not in text_out


async def test_grist_record_without_key_is_ignored():
    p = path_of("cites")
    w = world({p: doc("cites", entities=["prd:alpha"])}, [p])
    fake = FakeForgejoHttp(w)
    result = await run(fake, {**TABLES, "Products": [{"name": "alpha"}, "prd:other"]})
    assert result.lines == [
        f"{p}: entity not found: prd:alpha",
        f"{p}: product or component folder is not a known entity",
    ]


_RAW = yaml.safe_load((DATA / "cases.yaml").read_text())
_PASSING = sorted(n for n, c in _RAW["cases"].items() if not c["expect"])


@pytest.mark.parametrize("name", _PASSING)
async def test_agreement_passing_cases(name):
    case = _RAW["cases"][name]
    text = (DATA / f"{name}.md").read_text()
    tree = {pp: doc("x") for paths in _RAW["world"]["ids_in_repo"].values() for pp in paths}
    tree[case["path"]] = text
    others = {
        "canonical-hr": repo(
            {f"product/alpha/requirements/{i}.md": "" for i in _RAW["world"]["ids_elsewhere"]}
        )
    }
    fake = FakeForgejoHttp(world(tree, [case["path"]], others))
    tables = {
        "Products": _RAW["world"]["entity_keys"]["prd"],
        "Components": _RAW["world"]["entity_keys"]["cmp"],
        "Customers": _RAW["world"]["entity_keys"]["cus"],
        "Versions": [],
    }
    result = await run(fake, tables)
    assert result.lines == [] and result.ok
    assert isinstance(parse_document(text, case["path"], CONFIG.domains), ParsedDoc)


def test_main_exit_codes(monkeypatch, tmp_path):
    p = path_of("good")
    fake = FakeForgejoHttp(world({p: doc("good")}, [p]))

    def forgejo(url, token, org):
        return ForgejoClient(url, token, org, transport=fake.transport)

    def grist(url, doc_id, key):
        return GristClient(url, doc_id, key, transport=grist_transport(TABLES))

    monkeypatch.setattr(cli, "_make_forgejo", forgejo)
    monkeypatch.setattr(cli, "_make_grist", grist)
    token = tmp_path / "t"
    token.write_text("tok\n")
    base = [
        "validate",
        "--repo",
        "canonical-all",
        "--base",
        "b",
        "--head",
        "h",
        "--forgejo-url",
        "http://f",
        "--token-file",
        str(token),
        "--grist-url",
        "http://g",
        "--grist-key-file",
        str(token),
        "--grist-doc-id",
        "d",
    ]
    assert cli.main(base) == 0
    assert fake.statuses[-1]["state"] == "success"
    fake.fail["/compare/"] = 500
    assert cli.main(base) == 2
    assert fake.statuses[-1]["state"] == "error"
    assert cli.main([a for a in base if a != "--forgejo-url" and a != "http://f"]) == 2


def test_main_failure_exit_one_and_required(monkeypatch, tmp_path, capsys):
    p = path_of("bad")
    fake = FakeForgejoHttp(world({p: doc("bad", entities=["prd:ghost"])}, [p]))
    monkeypatch.setattr(
        cli, "_make_forgejo", lambda u, t, o: ForgejoClient(u, t, o, transport=fake.transport)
    )
    monkeypatch.setattr(
        cli, "_make_grist", lambda u, d, k: GristClient(u, d, k, transport=grist_transport(TABLES))
    )
    monkeypatch.delenv("KH_FORGEJO_URL", raising=False)
    token = tmp_path / "t"
    token.write_text("tok")
    argv = [
        "validate",
        "--repo",
        "canonical-all",
        "--base",
        "b",
        "--head",
        "h",
        "--token-file",
        str(token),
        "--grist-url",
        "http://g",
        "--grist-key-file",
        str(token),
        "--grist-doc-id",
        "d",
        "--no-status",
    ]
    assert cli.main(argv) == 2
    assert "validate: --forgejo-url is required" in capsys.readouterr().err
    assert cli.main([*argv, "--forgejo-url", "http://f"]) == 1
    assert "entity not found: prd:ghost" in capsys.readouterr().out
