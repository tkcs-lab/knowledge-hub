"""The static shell ships in the package and stays free of unsafe DOM and script patterns."""

from __future__ import annotations

import re
from importlib import resources

import pytest

pytestmark = pytest.mark.unit

FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write")


def _files():
    root = resources.files("knowledge_hub.panel").joinpath("static")
    return [f for f in root.iterdir() if f.is_file()]


def test_static_files_present_and_readable():
    names = {f.name for f in _files()}
    assert {"index.html", "panel.css", "app.js"} <= names
    for f in _files():
        assert f.read_bytes()


def test_scripts_free_of_unsafe_patterns():
    for f in _files():
        if f.name.endswith((".js", ".html")):
            text = f.read_text(encoding="utf-8")
            for bad in FORBIDDEN:
                assert bad not in text, f"{f.name}: {bad}"


def test_index_has_no_inline_code_or_style():
    text = resources.files("knowledge_hub.panel").joinpath("static/index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", text, re.I)
    assert not re.search(r"\sstyle\s*=", text, re.I)
    assert not re.search(r"<style", text, re.I)
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.I | re.S):
        assert m.group(1).strip() == ""


def _registered_routes():
    from pathlib import Path

    from starlette.routing import Route

    from knowledge_hub.config import GroupConfig, KhConfig
    from knowledge_hub.panel.app import ROUTE_BUILDERS, build_panel_app
    from knowledge_hub.panel.context import PanelContext
    from knowledge_hub.panel.settings import PanelSettings

    group = GroupConfig(members=["a"], approvers=["a"])
    cfg = KhConfig(people={"a": "a@example.test"}, groups={"all": group})
    ctx = PanelContext(
        settings=PanelSettings(env="dev", config_file=Path("kh.yaml"), dev_admin="a@example.test"),
        signer=None,
        upstream=None,
        load_config=lambda: cfg,
    )
    app = build_panel_app(ctx)
    assert ROUTE_BUILDERS
    found = []
    for route in app.routes:
        if isinstance(route, Route):
            pattern = re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(route.path)) + "$")
            found.append((pattern, set(route.methods or ())))
    return found


def _api_literals():
    out = []
    for f in _files():
        if not f.name.endswith(".js"):
            continue
        text = re.sub(r"\$\{[^}]*\}", "{id}", f.read_text(encoding="utf-8"))
        for m in re.finditer(r"""["'`](/api/[A-Za-z0-9_/{}.-]*)""", text):
            before = text[max(0, m.start() - 40) : m.start()]
            method = re.search(r"""["'](GET|POST|PUT|PATCH|DELETE)["']\s*,\s*$""", before)
            out.append((f.name, m.group(1), method.group(1) if method else None))
    return out


def test_api_literals_match_registered_routes():
    routes = _registered_routes()
    literals = _api_literals()
    assert literals
    for name, path, method in literals:
        concrete = path.replace("{id}", "x")
        matching = [methods for pattern, methods in routes if pattern.match(concrete)]
        assert matching, f"{name}: no route for {path}"
        if method:
            assert any(method in m for m in matching), f"{name}: {method} {path} not registered"


def test_labels_js_uses_existing_label_ops():
    from knowledge_hub.panel import ops

    text = resources.files("knowledge_hub.panel").joinpath("static/labels.js").read_text(encoding="utf-8")
    used = set(re.findall(r"""["'`](label\.[a-z_]+)["'`]""", text))
    assert used
    assert used <= set(ops.OPS)


@pytest.mark.parametrize("script", ["groups.js", "repos.js"])
def test_group_and_repo_scripts_use_existing_ops(script):
    from knowledge_hub.panel import ops

    text = resources.files("knowledge_hub.panel").joinpath(f"static/{script}").read_text(encoding="utf-8")
    used = set(re.findall(r"""(?:runOp\([^,]+,|op:)\s*["'`]([a-z_]+\.[a-z_]+)["'`]""", text))
    assert used
    assert used <= set(ops.OPS)


@pytest.mark.parametrize("script", ["pipeline.js", "health.js"])
def test_pipeline_and_health_scripts_use_existing_ops(script):
    from knowledge_hub.panel import ops

    text = resources.files("knowledge_hub.panel").joinpath(f"static/{script}").read_text(encoding="utf-8")
    used = set(re.findall(r"""["'`]([a-z]+\.[a-z_]+)["'`]""", text))
    assert used
    assert used <= set(ops.OPS)


def _text(name):
    return resources.files("knowledge_hub.panel").joinpath(f"static/{name}").read_text(encoding="utf-8")


def test_only_ops_js_posts_ops_and_sets_dry_run_false():
    for f in _files():
        if f.name.endswith(".js") and f.name != "ops.js":
            text = f.read_text(encoding="utf-8")
            assert "dry_run" not in text, f.name
            assert "/api/ops" not in text, f.name
    assert "dry_run: dryRun" in _text("ops.js")


def test_scripts_share_the_ops_flow_without_copies():
    assert "groups.js" not in _text("pipeline.js")
    assert "/static/ops.js" in _text("pipeline.js")
    for name in ("groups.js", "repos.js", "pipeline.js"):
        text = _text(name)
        assert "setTimeout" not in text, f"{name} has its own poll loop"
        assert "runOp" not in text and "argsForm" not in text, name
    for name in ("groups.js", "repos.js"):
        assert "/api/ops" not in _text(name)


def test_pipeline_ops_without_dry_run_are_previewless():
    text = _text("pipeline.js")
    assert "previewless: true" in text
    for op in ("jobs.retry", "repo.rebuild", "jobs.reclaim", "embeddings.requeue"):
        assert op in text


def test_busy_answer_maps_to_busy_message():
    text = _text("app.js")
    assert "409" in text and "BUSY_MESSAGE" in text


def test_repo_set_omits_empty_dirs():
    text = _text("repos.js")
    assert re.search(r'name: "dirs"[^}]*omitEmpty: true', text)
    assert "splitList(dirs" not in text
