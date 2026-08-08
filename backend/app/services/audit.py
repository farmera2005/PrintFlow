from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog


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
