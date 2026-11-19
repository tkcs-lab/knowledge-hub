from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["tests.support.fixtures_db", "tests.support.fixtures_app", "tests.support.oauth_app"]

_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if path.is_relative_to(_ROOT / "integration"):
            item.add_marker(pytest.mark.integration)
        elif path.is_relative_to(_ROOT / "unit"):
            item.add_marker(pytest.mark.unit)
