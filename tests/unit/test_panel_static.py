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
