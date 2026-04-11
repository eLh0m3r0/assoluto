#!/usr/bin/env bash
# Container entrypoint:
#   1. Wait for Postgres to be reachable (best-effort, 30s).
#   2. Apply Alembic migrations.
#   3. Exec the CMD (uvicorn).
set -euo pipefail

run_migrations() {
    echo "[entrypoint] Running alembic upgrade head..."
    alembic upgrade head
}

wait_for_postgres() {
    python - <<'PY'
import os
import sys
import time
from urllib.parse import urlparse

import psycopg

url = os.environ.get("DATABASE_SYNC_URL")
if not url:
    print("[entrypoint] DATABASE_SYNC_URL not set, skipping wait", flush=True)
    sys.exit(0)

parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://"))
host = parsed.hostname or "localhost"
port = parsed.port or 5432

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=2):
            print(f"[entrypoint] Postgres reachable at {host}:{port}", flush=True)
            sys.exit(0)
    except Exception as exc:
        print(f"[entrypoint] Waiting for Postgres... ({exc})", flush=True)
        time.sleep(1)

print("[entrypoint] Postgres did not become ready in 30s", flush=True)
sys.exit(1)
PY
}

wait_for_postgres
run_migrations
exec "$@"
