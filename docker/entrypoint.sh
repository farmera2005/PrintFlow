#!/bin/sh
set -e

echo "PrintFlow: waiting for the database..."
python - <<'PY'
import os, socket, sys, time
from urllib.parse import urlparse

url = os.environ.get("DATABASE_URL", "")
parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
host = parsed.hostname or "postgres"
port = parsed.port or 5432

deadline = time.time() + 120
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"PrintFlow: database reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"PrintFlow: could not reach the database at {host}:{port}", file=sys.stderr)
sys.exit(1)
PY

echo "PrintFlow: applying database migrations..."
alembic upgrade head

echo "PrintFlow: starting."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
