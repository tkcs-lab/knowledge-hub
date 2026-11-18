"""The serving app must not pull in the admin client or the setup package (plan 3.7)."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_app_import_leaves_admin_code_out():
    code = (
        "import sys, knowledge_hub.app\n"
        "bad = [m for m in sys.modules if m == 'knowledge_hub.forgejo.admin' "
        "or m == 'knowledge_hub.setup' or m.startswith('knowledge_hub.setup.') "
        "or m == 'knowledge_hub.admin' or m.startswith('knowledge_hub.admin.') "
        "or m == 'ruamel' or m.startswith('ruamel.')]\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
