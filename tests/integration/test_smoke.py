import pytest

pytestmark = pytest.mark.integration


def test_package_imports():
    import knowledge_hub

    assert knowledge_hub.__version__ == "0.1.0"
