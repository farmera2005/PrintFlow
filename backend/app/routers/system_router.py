"""Sync log, audit trail, and runtime settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import AuditLog, SyncLog, User
from ..services import credentials
from ..services.settings_store import (
    KEY_PUBLIC_BASE_URL,
    get_poll_intervals,
    get_setting,
    is_setup_complete,
)

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/sync-log")
async def sync_log(
    job: str | None = None,
    only_failures: bool = False,
    limit: int = 200,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(SyncLog).order_by(SyncLog.started_at.desc())
    if job:
        stmt = stmt.where(SyncLog.job == job)
    if only_failures:
        stmt = stmt.where(SyncLog.ok.is_(False))
    rows = (await session.execute(stmt.limit(max(1, min(limit, 1000))))).scalars().all()
    return {
        "entries": [
            {
                "id": row.id,
                "job": row.job,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "ok": row.ok,
                "detail": row.detail,
            }
            for row in rows
        ]
    }


@router.get("/audit-log")
async def audit_log(
    limit: int = 200,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc())
                .limit(max(1, min(limit, 1000)))
            )
        )
        .scalars()
        .all()
    )
    return {
        "entries": [
            {
                "id": row.id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "action": row.action,
                "detail": row.detail,
                "actor": row.actor,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/settings")
async def app_settings(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    from ..config import get_config
    from ..scheduler import job_overview

    config = get_config()
    return {
        "version": {"git_sha": config.git_sha, "built_at": config.built_at},
        "setup_complete": await is_setup_complete(session),
        "poll_intervals": await get_poll_intervals(session),
        "public_base_url": await get_setting(session, KEY_PUBLIC_BASE_URL),
        "integrations": await credentials.status_summary(session),
        "jobs": job_overview(),
    }


@router.get("/health")
async def health() -> dict:
    """Container liveness plus enough to answer "is my update actually live?".

    Unauthenticated on purpose: when the UI looks wrong, the first thing worth
    knowing is which build is answering, and needing to log in to find that out
    is exactly backwards.
    """
    from ..config import get_config
    from ..services import tunnel

    config = get_config()
    return {
        "ok": True,
        "version": config.git_sha,
        "built_at": config.built_at,
        # Present only in builds that include the Cloudflare Tunnel feature, so
        # its absence dates the container immediately.
        "features": {
            "cloudflare_tunnel": True,
            "cloudflared_installed": tunnel.available(),
        },
    }
