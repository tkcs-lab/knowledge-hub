#!/usr/bin/env bash
# Idempotent: make sure PostgreSQL 16 and pgvector are installed for the test cluster.
set -euo pipefail
INITDB=/usr/lib/postgresql/16/bin/initdb
VEC=/usr/share/postgresql/16/extension/vector.control
if [ -x "$INITDB" ] && [ -f "$VEC" ]; then
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y postgresql-16 postgresql-16-pgvector
[ -x "$INITDB" ] && [ -f "$VEC" ]
