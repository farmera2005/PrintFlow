#!/bin/sh
set -e

DATA_DIR="${PRINTFLOW_DATA_DIR:-/data}"

# The container runs unprivileged, so it cannot chown its own volume. A volume
# created before the image declared /data is owned by root, and the app would
# die on the encryption key with a traceback instead of something actionable.
if [ ! -d "$DATA_DIR" ] || [ ! -w "$DATA_DIR" ]; then
  echo "PrintFlow: cannot write to $DATA_DIR (running as uid $(id -u))." >&2
  echo >&2
  echo "  That directory holds the encryption key for your stored credentials," >&2
  echo "  so it has to persist and be writable. Fix the ownership once with:" >&2
  echo >&2
  echo "    docker compose run --rm --user root --entrypoint sh app \\" >&2
  echo "      -c 'chown -R 10001 $DATA_DIR'" >&2
  echo "    docker compose up -d" >&2
  echo >&2
  exit 1
fi

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

# app.server runs both listeners and generates the secret key and a self-signed
# certificate on first boot, so there is nothing to prepare before this point.
echo "PrintFlow: starting."
exec python -m app.server
