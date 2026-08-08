"""Every background job records a sync_log row — success or failure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SyncLog


class RunRecord:
    """Mutable handle a job uses to describe what it did."""

    def __init__(self, row: SyncLog) -> None:
        self.row = row
        self._notes: list[str] = []

    def note(self, message: str) -> None:
        self._notes.append(message)

    @property
    def detail(self) -> str:
        return "; ".join(self._notes)


@asynccontextmanager
async def run(session: AsyncSession, job: str) -> AsyncIterator[RunRecord]:
    """Bracket a job run. Failures are logged and re-raised for the caller to swallow."""
    row = SyncLog(job=job, started_at=datetime.now(timezone.utc))
    session.add(row)
    await session.flush()
    record = RunRecord(row)
    try:
        yield record
    except Exception as exc:
        row.ok = False
        row.finished_at = datetime.now(timezone.utc)
        detail = record.detail
        row.detail = (f"{detail}; " if detail else "") + f"{type(exc).__name__}: {exc}"[:4000]
        await session.flush()
        raise
    else:
        row.ok = True
        row.finished_at = datetime.now(timezone.utc)
        row.detail = record.detail[:4000] or None
        await session.flush()
