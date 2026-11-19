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


def test_app_import_leaves_directory_sync_code_out():
    code = (
        "import sys, knowledge_hub.app\n"
        "banned = ('knowledge_hub.directory.sync', 'knowledge_hub.directory.cli', "
        "'knowledge_hub.directory.google', 'knowledge_hub.directory.keycloak', "
        "'knowledge_hub.auth.keys_cli')\n"
        "bad = [m for m in sys.modules if m in banned]\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_import_leaves_panel_out():
    code = (
        "import sys, knowledge_hub.app\n"
        "bad = [m for m in sys.modules if m == 'knowledge_hub.panel' "
        "or m.startswith('knowledge_hub.panel.')]\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_panel_app_import_leaves_hub_code_out():
    code = (
        "import sys, knowledge_hub.panel.app\n"
        "bad = [m for m in sys.modules if m == 'knowledge_hub.app' or m == 'knowledge_hub.mcp' "
        "or m.startswith('knowledge_hub.mcp.') or m == 'knowledge_hub.admin' "
        "or m.startswith('knowledge_hub.admin.') or m == 'ruamel' or m.startswith('ruamel.')]\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
