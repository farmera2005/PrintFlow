#!/bin/sh
# Update PrintFlow in place: pull, rebuild, restart.
#
#   ./update.sh
#
# Your data is in Docker volumes (printflow-db, printflow-data), not in the
# image, so rebuilding does not touch it. Database migrations are applied
# automatically when the container starts.
set -e

cd "$(dirname "$0")"

if [ ! -d .git ]; then
  echo "error: this is not a git checkout, so there is nothing to pull." >&2
  echo "       Clone the repository instead of downloading a zip." >&2
  exit 1
fi

# Local edits to tracked files would make the pull fail halfway. Say so up front
# rather than leaving a half-updated checkout behind.
if ! git diff --quiet HEAD -- 2>/dev/null; then
  echo "error: you have local changes to tracked files:" >&2
  git --no-pager diff --name-only HEAD -- | sed 's/^/         /' >&2
  echo >&2
  echo "       Put deployment tweaks in docker-compose.override.yml instead —" >&2
  echo "       it is gitignored, so updates never conflict with it:" >&2
  echo "         cp docker-compose.override.yml.example docker-compose.override.yml" >&2
  echo "         git checkout -- docker-compose.yml" >&2
  exit 1
fi

BEFORE=$(git rev-parse --short HEAD)
echo "PrintFlow: currently on $BEFORE. Pulling..."

# --ff-only: refuse to create a surprise merge commit on someone's NAS.
if ! git pull --ff-only; then
  echo >&2
  echo "error: could not fast-forward. Your checkout has diverged from the" >&2
  echo "       remote. Resolve it by hand, or re-clone if you have no local" >&2
  echo "       commits worth keeping." >&2
  exit 1
fi

AFTER=$(git rev-parse --short HEAD)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "PrintFlow: already up to date ($AFTER). Rebuilding anyway."
else
  echo "PrintFlow: $BEFORE -> $AFTER"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/  /'
fi

# Stamped into the image so the running version is visible in Settings —
# the quickest way to confirm an update actually took.
GIT_SHA=$AFTER
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
export GIT_SHA BUILD_TIME

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

echo "PrintFlow: rebuilding and restarting..."
$COMPOSE up -d --build

echo "PrintFlow: tidying up old images..."
docker image prune -f >/dev/null 2>&1 || true

echo
echo "PrintFlow: now running $GIT_SHA. Settings shows the running version."
