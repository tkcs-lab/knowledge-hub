import re

import pytest

pytestmark = pytest.mark.integration


def test_package_imports():
    import knowledge_hub

    assert re.fullmatch(r"\d+\.\d+\.\d+", knowledge_hub.__version__)
