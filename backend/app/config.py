"""Process configuration.

Only two environment variables are read: DATABASE_URL and SECRET_KEY. Every
other piece of configuration — including all integration credentials — lives in
the database and is entered through the in-app setup wizard.
"""

from __future__ import annotations

import os
from functools import lru_cache


class Config:
    def __init__(self) -> None:
        self.database_url: str = _normalize_db_url(
            os.getenv("DATABASE_URL", "postgresql+asyncpg://printflow:printflow@localhost:5432/printflow")
        )
        self.secret_key: str = os.getenv("SECRET_KEY", "")
        if not self.secret_key:
            # A missing key would silently rotate on every restart, invalidating
            # sessions and — far worse — making stored credentials undecryptable.
            raise RuntimeError(
                "SECRET_KEY is not set. Set it in docker-compose (any long random string) "
                "and keep it stable: stored integration credentials are encrypted with a key "
                "derived from it."
            )
        self.session_cookie: str = "printflow_session"
        self.session_max_age: int = 60 * 60 * 24 * 30
        # Static frontend build, served by the same container in production.
        self.static_dir: str = os.getenv(
            "STATIC_DIR", os.path.join(os.path.dirname(__file__), "static")
        )


def _normalize_db_url(url: str) -> str:
    """Accept the plain libpq URL that most tooling emits and force the async driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
