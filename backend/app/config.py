"""Process configuration.

The goal is a zero-edit install: `docker compose up -d` should work without
touching a single file. So nothing here is *required* to be set.

- DATABASE_URL   defaults to the compose service, override if you host PG yourself.
- SECRET_KEY     read from the environment if set; otherwise generated once and
                 persisted to the data volume, so it survives restarts. It is the
                 key stored credentials are encrypted with, which is exactly why
                 it must be stable — and why generating-and-persisting is safer
                 than asking an operator to remember to change a placeholder.

Everything else — integration credentials, poll intervals, the TLS certificate —
is configured in the app's setup wizard and lives in the database.
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("printflow.config")

SECRET_KEY_FILENAME = "secret_key"
SECRET_KEY_BYTES = 48

# Where the persisted secret key and the materialised TLS files live. `/data` is
# the volume mount inside the container; the fallback keeps bare-metal dev from
# needing root.
DATA_DIR_CANDIDATES = ("/data", "./.printflow-data")


class Config:
    def __init__(self) -> None:
        self.data_dir: Path = _resolve_data_dir()
        self.database_url: str = _normalize_db_url(
            os.getenv(
                "DATABASE_URL",
                "postgresql+asyncpg://printflow:printflow@localhost:5432/printflow",
            )
        )
        self.secret_key, self.secret_key_source = _resolve_secret_key(self.data_dir)

        self.http_port: int = _int_env("PRINTFLOW_HTTP_PORT", 8000)
        self.https_port: int = _int_env("PRINTFLOW_HTTPS_PORT", 8443)
        self.tls_dir: Path = self.data_dir / "tls"

        # Stamped into the image at build time; "unknown" outside Docker.
        self.git_sha: str = os.getenv("PRINTFLOW_GIT_SHA", "").strip() or "unknown"
        self.built_at: str = os.getenv("PRINTFLOW_BUILT_AT", "").strip() or "unknown"

        self.session_cookie: str = "printflow_session"
        self.session_max_age: int = 60 * 60 * 24 * 30
        # Static frontend build, served by the same container in production.
        self.static_dir: str = os.getenv(
            "STATIC_DIR", os.path.join(os.path.dirname(__file__), "static")
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _normalize_db_url(url: str) -> str:
    """Accept the plain libpq URL that most tooling emits and force the async driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _resolve_data_dir() -> Path:
    explicit = os.getenv("PRINTFLOW_DATA_DIR")
    candidates = [explicit] if explicit else list(DATA_DIR_CANDIDATES)
    problems: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.touch()
            probe.unlink()
            return path.resolve()
        except OSError as exc:
            problems.append(f"{path}: {exc}")
    raise RuntimeError(
        "No writable data directory. PrintFlow stores its generated secret key "
        "and TLS files there, so it must persist across restarts. Set "
        "PRINTFLOW_DATA_DIR to a writable path (in Docker this is the mounted "
        "volume at /data). Tried: " + "; ".join(problems)
    )


def _resolve_secret_key(data_dir: Path) -> tuple[str, str]:
    """Return (key, source). Generates and persists one on first run."""
    from_env = os.getenv("SECRET_KEY", "").strip()
    if from_env:
        return from_env, "environment"

    path = data_dir / SECRET_KEY_FILENAME
    existing = _read_secret_key(path)
    if existing:
        return existing, "file"

    candidate = secrets.token_urlsafe(SECRET_KEY_BYTES)
    try:
        # O_EXCL so two workers starting at once cannot each write a different
        # key and leave half the stored credentials undecryptable.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = _read_secret_key(path)
        if existing:
            return existing, "file"
        raise RuntimeError(f"Secret key file {path} exists but is empty; delete it and restart.")
    except OSError as exc:
        raise RuntimeError(
            f"Could not write the secret key to {path}: {exc}. Without a stable key, "
            "stored integration credentials cannot be decrypted after a restart."
        ) from exc

    with os.fdopen(fd, "w") as handle:
        handle.write(candidate)
    log.info("Generated a new secret key at %s", path)
    return candidate, "generated"


def _read_secret_key(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


@lru_cache(maxsize=1)
def get_config() -> Config:
    return get_config_uncached()


def get_config_uncached() -> Config:
    return Config()
