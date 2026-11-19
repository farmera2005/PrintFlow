"""Sync log, audit trail, and runtime settings."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import AuditLog, Order, SyncLog, User
from ..services import audit, credentials
from ..services.settings_store import (
    FLAG_COLORS,
    FLAG_LABEL_MAX,
    FLAG_LIMIT,
    KEY_PUBLIC_BASE_URL,
    get_order_flags,
    get_poll_intervals,
    get_setting,
    is_setup_complete,
    set_order_flags,
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


async def _flag_usage(session: AsyncSession) -> dict[str, int]:
    """How many orders carry each flag.

    One column of one table, tallied here rather than in SQL: the query is
    the same shape on every database, and the only moment anybody asks is
    when Settings is open and somebody is deciding whether a flag is safe to
    delete. A shop big enough for this to be the wrong trade would have told
    us long before, by being slow at something that matters more.
    """
    rows = (await session.execute(select(Order.flags))).scalars().all()
    tally: Counter[str] = Counter()
    for flags in rows:
        tally.update(str(flag) for flag in (flags or []))
    return dict(tally)


class OrderFlagInput(BaseModel):
    """One flag as the Settings screen sends it back.

    The id is optional and comes back untouched when it is there: a flag the
    screen already knew about keeps the id the orders are carrying, and one
    with no id is being invented right now and gets one.
    """

    id: str | None = Field(default=None, max_length=48)
    label: str = Field(max_length=FLAG_LABEL_MAX)
    color: str | None = None


class OrderFlagsRequest(BaseModel):
    flags: list[OrderFlagInput] = Field(default_factory=list, max_length=FLAG_LIMIT)


@router.get("/settings/order-flags")
async def read_order_flags(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """The shop's flags, what they cost to delete, and what colours exist."""
    usage = await _flag_usage(session)
    flags = await get_order_flags(session)
    known = {flag["id"] for flag in flags}
    return {
        "flags": [{**flag, "orders": usage.get(flag["id"], 0)} for flag in flags],
        # Flags that orders carry but Settings no longer has. Deleting a flag
        # does not go and edit the orders that had it, so this is the only
        # place that leftover is visible — and the only way somebody can
        # decide to bring the flag back rather than wonder where it went.
        "orphans": [
            {"id": flag_id, "orders": count}
            for flag_id, count in sorted(usage.items())
            if flag_id not in known
        ],
        "colors": list(FLAG_COLORS),
        "limit": FLAG_LIMIT,
        "label_max": FLAG_LABEL_MAX,
    }


@router.put("/settings/order-flags")
async def write_order_flags(
    body: OrderFlagsRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace the shop's flag list with this one, in this order.

    Orders are not touched. A flag deleted here stops being drawn on the
    orders that carry it and is dropped from each of them the next time
    somebody edits that order's flags — which is the forgiving direction to
    be wrong in: bringing the flag back brings the orders back with it.
    """
    before = {flag["id"]: flag["label"] for flag in await get_order_flags(session)}
    try:
        flags = await set_order_flags(
            session, [flag.model_dump() for flag in body.flags]
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    after = {flag["id"]: flag["label"] for flag in flags}
    usage = await _flag_usage(session)
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="order_flags_changed",
        # Deletions carry their cost, because that is the change somebody
        # might want to undo and the one the log should make obvious.
        detail={
            "added": [after[key] for key in after.keys() - before.keys()],
            "removed": [
                {"label": before[key], "orders": usage.get(key, 0)}
                for key in before.keys() - after.keys()
            ],
        },
        actor=user.username,
    )
    await session.commit()
    return {
        "flags": [{**flag, "orders": usage.get(flag["id"], 0)} for flag in flags],
        "colors": list(FLAG_COLORS),
        "limit": FLAG_LIMIT,
        "label_max": FLAG_LABEL_MAX,
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
