from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog


async def find(
    session: AsyncSession, *, action: str, key: str, value: str
) -> AuditLog | None:
    """The most recent record of this action carrying this detail, if any.

    Written for one job: telling "do this" apart from "do this again" when the
    operator cannot tell either, because the answer to the first attempt went
    missing somewhere between here and their browser. The audit log already
    records what was done, so it is also the record of what not to do twice.

    Matched on one key read out of the JSON rather than with a containment
    operator, because the column is a JSON/JSONB variant and containment
    compiles to a LIKE against the serialised text on anything that is not
    Postgres — which would match by accident and, far worse, could fail to
    match and print a second plate.
    """
    found = await session.execute(
        select(AuditLog)
        .where(
            AuditLog.action == action,
            AuditLog.detail[key].as_string() == value,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    return found.scalars().first()


async def record(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: Any,
    action: str,
    detail: dict[str, Any] | None = None,
    actor: str | None = None,
) -> AuditLog:
    row = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        detail=detail or {},
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row
