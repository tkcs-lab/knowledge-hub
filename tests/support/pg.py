"""A throwaway PostgreSQL 16 cluster on a unix socket for integration tests."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import psycopg

PG_BIN = Path("/usr/lib/postgresql/16/bin")
REPO_ROOT = Path(__file__).resolve().parents[2]
FRESH_DB = "kh_fresh"
TEMPLATE_DB = "kh_template"
LOGIN_ROLES = ("kh_migrator", "kh_app", "kh_indexer")


class Cluster:
    def __init__(self) -> None:
        self.root = Path(f"/var/tmp/kh-pg-{os.getpid()}")
        self.data = self.root / "data"
        self.sock = self.root / "sock"
        self.started = False

    def _run(self, binary: str, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [str(PG_BIN / binary), *args]
        if os.geteuid() == 0:
            cmd = ["su", "postgres", "-s", "/bin/bash", "-c", shlex.join(cmd)]
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    def start(self) -> None:
        if not (PG_BIN / "initdb").exists():
            subprocess.run(["bash", str(REPO_ROOT / "scripts" / "ensure_test_pg.sh")], check=True)
        shutil.rmtree(self.root, ignore_errors=True)
        self.sock.mkdir(parents=True)
        if os.geteuid() == 0:
            shutil.chown(self.root, "postgres", "postgres")
            shutil.chown(self.sock, "postgres", "postgres")
        self._run("initdb", "-D", str(self.data), "-A", "trust", "-U", "postgres")
        opts = f"-c listen_addresses= -c unix_socket_directories={self.sock} -c fsync=off"
        self._run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "log"), "-w", "-o", opts, "start")
        self.started = True

    def stop(self) -> None:
        if self.started:
            self._run("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop")
            self.started = False
        shutil.rmtree(self.root, ignore_errors=True)

    def admin_dsn(self, dbname: str = "postgres") -> str:
        return f"host={self.sock} port=5432 user=postgres dbname={dbname}"

    def dsn(self, role: str, dbname: str = "kh") -> str:
        return f"host={self.sock} port=5432 user={role} dbname={dbname}"


def bootstrap_roles(cluster: Cluster) -> None:
    """Create roles, database ``kh`` and the vector extension as the sandbox superuser."""
    with psycopg.connect(cluster.admin_dsn(), autocommit=True) as conn:
        conn.execute("CREATE ROLE kh_bi NOLOGIN NOSUPERUSER NOBYPASSRLS")
        for role in LOGIN_ROLES:
            conn.execute(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS")
        conn.execute("CREATE DATABASE kh OWNER kh_migrator")
    with psycopg.connect(cluster.admin_dsn("kh"), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
