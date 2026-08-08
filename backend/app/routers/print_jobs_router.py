"""Print Queue — a flat view of every print job, with re-queue and cancel."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations.base import IntegrationError
from ..models import PrintJob, User
from ..services import audit, printing
from ..services.credentials import IntegrationNotConfigured

router = APIRouter(prefix="/api/print-jobs", tags=["print-jobs"])


@router.get("")
async def list_print_jobs(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    return {"jobs": await printing.open_jobs_overview(session)}


async def _get_job(session: AsyncSession, job_id: uuid.UUID) -> PrintJob:
    job = await session.get(PrintJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Print job not found")
    return job


@router.post("/{job_id}/requeue")
async def requeue(
    job_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await _get_job(session, job_id)
    try:
        await printing.requeue_job(session, job)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await audit.record(
        session,
        entity_type="print_job",
        entity_id=job.id,
        action="requeue",
        actor=user.username,
    )
    await session.commit()
    return {"id": job.id, "status": job.status, "error": job.error}


@router.post("/{job_id}/cancel")
async def cancel(
    job_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await _get_job(session, job_id)
    await printing.cancel_job(session, job)
    await audit.record(
        session,
        entity_type="print_job",
        entity_id=job.id,
        action="cancel",
        actor=user.username,
    )
    await session.commit()
    return {"id": job.id, "status": job.status}


@router.post("/dispatch")
async def dispatch(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Push every pending plate to Bambuddy right now."""
    try:
        stats = await printing.dispatch_pending(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return stats
