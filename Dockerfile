# --- Stage 1: build the React frontend -------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRINTFLOW_DATA_DIR=/data \
    HOME=/home/printflow

WORKDIR /app

# cloudflared, so a Cloudflare Tunnel can be set up entirely from the wizard
# rather than needing a second container. Best-effort: if the download fails
# (offline build, GitHub unreachable) the image still works and the app says
# plainly that the binary is missing instead of failing to start.
#
# Downloaded with Python rather than curl on purpose: installing curl meant an
# apt round-trip, and `apt-get purge --auto-remove curl` can take
# ca-certificates with it (apt treats it as a dependency it pulled in), which
# would quietly break every outbound HTTPS call to Etsy and QuickBooks. Python
# is already here and needs no cleanup.
ARG TARGETARCH=amd64
ARG CLOUDFLARED_VERSION=latest
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in amd64|arm64|arm) ;; *) arch=amd64 ;; esac; \
    if [ "$CLOUDFLARED_VERSION" = "latest" ]; then \
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"; \
    else \
      url="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${arch}"; \
    fi; \
    ( python -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], '/usr/local/bin/cloudflared')" "$url" \
      && chmod +x /usr/local/bin/cloudflared \
      && /usr/local/bin/cloudflared --version ) \
      || { \
        echo "WARNING: cloudflared was not bundled; set the tunnel up externally"; \
        rm -f /usr/local/bin/cloudflared; \
      }
# The rm matters: a partial download or a wrong-architecture binary would still
# look "installed" to the app, which would then report the tunnel as available
# and fail confusingly at runtime instead of saying it is missing.

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
# The built SPA is served by the same container, so there is a single origin
# and no CORS configuration to get wrong.
COPY --from=frontend /build/dist ./app/static

# Stamped by update.sh so the running commit is visible in Settings — the
# quickest way to tell whether a `git pull` actually reached the container.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV PRINTFLOW_GIT_SHA=$GIT_SHA \
    PRINTFLOW_BUILT_AT=$BUILD_TIME

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --uid 10001 printflow \
    && mkdir -p /data \
    && chown -R printflow /app /data
# An empty named volume mounted at /data inherits this ownership, so the
# generated secret key and TLS files are writable without running as root.
USER printflow

# 8000 HTTP, 8443 HTTPS. Both are served by the same process.
EXPOSE 8000 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
