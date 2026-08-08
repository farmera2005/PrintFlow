"""Background jobs (§6).

Every job logs to sync_log, records per-integration health for the UI banners,
and swallows integration failures so a flaky third party never takes the app
down.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .db import session_scope
from .integrations import etsy as etsy_api
from .integrations import qbo as qbo_api
from .integrations.base import IntegrationError
from .models import (
    PROVIDER_BAMBUDDY,
    PROVIDER_ETSY,
    PROVIDER_QBO,
    PROVIDER_SHIPSTATION,
    Order,
)
from .services import credentials, intake, printing, shipping, sync_log
from .services.credentials import IntegrationNotConfigured
from .services.settings_store import get_poll_intervals, is_setup_complete

log = logging.getLogger("printflow.scheduler")

JOB_ETSY = "etsy_receipt_poll"
JOB_BAMBUDDY = "bambuddy_status_reconcile"
JOB_SHIPSTATION = "shipstation_order_match"
JOB_QBO = "qbo_token_refresh"

# The QBO refresh job is cheap and has no configurable interval; it only acts
# when the access token is within 10 minutes of expiring.
QBO_CHECK_MINUTES = 5

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


async def poll_etsy() -> dict[str, Any]:
    async with session_scope() as session:
        if not await is_setup_complete(session):
            return {"skipped": "setup incomplete"}
        async with sync_log.run(session, JOB_ETSY) as record:
            await session.commit()
            try:
                client = await etsy_api.client_for(session)
            except IntegrationNotConfigured:
                record.note("Etsy not connected")
                await session.commit()
                return {"skipped": "not connected"}
            if not client.shop_id:
                record.note("No Etsy shop selected")
                await session.commit()
                return {"skipped": "no shop"}
            try:
                receipts = await client.iter_receipts(shop_id=client.shop_id)
            except IntegrationError as exc:
                await credentials.mark_error(session, PROVIDER_ETSY, str(exc))
                await session.commit()
                raise
            stats = await intake.ingest_many(session, receipts)
            await credentials.mark_ok(session, PROVIDER_ETSY)
            record.note(
                f"{stats['seen']} receipts: {stats['created']} new, "
                f"{stats['skipped']} already known, {stats['errors']} errors"
            )
            await session.commit()

    # Newly planned plates go out on the next line, so a fresh order does not
    # wait a full Bambuddy cycle before printing starts.
    try:
        await dispatch_prints()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Post-intake dispatch failed: %s", exc)
    return stats


async def dispatch_prints() -> dict[str, Any]:
    async with session_scope() as session:
        try:
            stats = await printing.dispatch_pending(session)
        except IntegrationNotConfigured:
            return {"skipped": "not connected"}
        await session.commit()
        return stats


async def reconcile_bambuddy() -> dict[str, Any]:
    async with session_scope() as session:
        if not await is_setup_complete(session):
            return {"skipped": "setup incomplete"}
        async with sync_log.run(session, JOB_BAMBUDDY) as record:
            await session.commit()
            try:
                dispatched = await printing.dispatch_pending(session)
                stats = await printing.reconcile(session)
            except IntegrationNotConfigured:
                record.note("Bambuddy not connected")
                await session.commit()
                return {"skipped": "not connected"}
            except IntegrationError as exc:
                await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
                await session.commit()
                raise
            await credentials.mark_ok(session, PROVIDER_BAMBUDDY)
            record.note(
                f"dispatched {dispatched['dispatched']}, checked {stats['checked']}, "
                f"advanced {stats['advanced']} ({stats['done']} done, {stats['failed']} failed)"
            )
            await session.commit()
            return {**stats, **dispatched}


async def match_shipstation() -> dict[str, Any]:
    async with session_scope() as session:
        if not await is_setup_complete(session):
            return {"skipped": "setup incomplete"}
        pending = (
            await session.execute(
                select(Order.id).where(Order.shipstation_order_id.is_(None)).limit(1)
            )
        ).first()
        if not pending:
            return {"skipped": "nothing to match"}
        async with sync_log.run(session, JOB_SHIPSTATION) as record:
            await session.commit()
            try:
                stats = await shipping.match_orders(session)
            except IntegrationNotConfigured:
                record.note("ShipStation not connected")
                await session.commit()
                return {"skipped": "not connected"}
            except IntegrationError as exc:
                await credentials.mark_error(session, PROVIDER_SHIPSTATION, str(exc))
                await session.commit()
                raise
            await credentials.mark_ok(session, PROVIDER_SHIPSTATION)
            record.note(
                f"checked {stats['checked']}, matched {stats['matched']}, "
                f"still missing {stats['not_found']}"
            )
            await session.commit()
            return stats


async def refresh_qbo_token() -> dict[str, Any]:
    """Refresh proactively at <10 minutes remaining (§6)."""
    async with session_scope() as session:
        try:
            client = await qbo_api.client_for(session)
        except IntegrationNotConfigured:
            return {"skipped": "not connected"}
        remaining = client.seconds_until_expiry()
        if remaining > qbo_api.REFRESH_MARGIN_SECONDS:
            return {"skipped": f"{int(remaining)}s remaining"}
        async with sync_log.run(session, JOB_QBO) as record:
            await session.commit()
            try:
                await client.refresh()
            except IntegrationError as exc:
                await credentials.mark_error(session, PROVIDER_QBO, str(exc))
                await session.commit()
                raise
            await credentials.mark_ok(session, PROVIDER_QBO)
            record.note("Access token refreshed")
            await session.commit()
            return {"refreshed": True}


JOBS: dict[str, Callable[[], Coroutine[Any, Any, dict[str, Any]]]] = {
    JOB_ETSY: poll_etsy,
    JOB_BAMBUDDY: reconcile_bambuddy,
    JOB_SHIPSTATION: match_shipstation,
    JOB_QBO: refresh_qbo_token,
}

PROVIDER_JOBS = {
    PROVIDER_ETSY: JOB_ETSY,
    PROVIDER_BAMBUDDY: JOB_BAMBUDDY,
    PROVIDER_SHIPSTATION: JOB_SHIPSTATION,
    PROVIDER_QBO: JOB_QBO,
}


async def _guarded(job_id: str) -> None:
    """APScheduler entry point — a failing job must never kill the scheduler."""
    try:
        await JOBS[job_id]()
    except Exception as exc:
        log.warning("Background job %s failed: %s", job_id, exc)


async def run_job_now(provider_or_job: str) -> dict[str, Any]:
    """Backs the "retry now" button on the integration failure banner."""
    job_id = PROVIDER_JOBS.get(provider_or_job, provider_or_job)
    if job_id not in JOBS:
        return {"error": f"Unknown job '{provider_or_job}'"}
    try:
        result = await JOBS[job_id]()
    except Exception as exc:
        return {"job": job_id, "ok": False, "error": str(exc)}
    return {"job": job_id, "ok": True, "result": result}


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


async def configure_jobs() -> None:
    scheduler = get_scheduler()
    async with session_scope() as session:
        intervals = await get_poll_intervals(session)

    plan = {
        JOB_ETSY: intervals["etsy_minutes"],
        JOB_BAMBUDDY: intervals["bambuddy_minutes"],
        JOB_SHIPSTATION: intervals["shipstation_minutes"],
        JOB_QBO: QBO_CHECK_MINUTES,
    }
    for job_id, minutes in plan.items():
        scheduler.add_job(
            _guarded,
            "interval",
            minutes=minutes,
            id=job_id,
            args=[job_id],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )


async def start() -> None:
    await configure_jobs()
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()


async def reschedule() -> None:
    """Apply new poll intervals without a restart."""
    await configure_jobs()


async def shutdown() -> None:
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)


def job_overview() -> list[dict[str, Any]]:
    scheduler = get_scheduler()
    if not scheduler.running:
        return []
    return [
        {
            "id": job.id,
            "next_run_at": getattr(job, "next_run_time", None),
            "interval": str(job.trigger),
        }
        for job in scheduler.get_jobs()
    ]
