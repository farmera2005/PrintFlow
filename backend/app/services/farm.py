"""The print farm as a screen: every machine, and the plates PrintFlow put on it.

A flat queue answers "what is outstanding". Standing in the shop the question is
almost always the other one — *which machine should I be looking at* — and that
is a question about the farm, not about the order book. So the plates are
grouped under the machine they were sent to, live status and all, and the ones
that have no machine yet get their own group rather than being invisible.

Bambuddy is the authority on what a machine is doing; PrintFlow is the authority
on what it was asked to make. Neither can answer alone: Bambuddy does not know
which order a plate belongs to, and PrintFlow does not know whether the printer
is even switched on. This joins the two and, where Bambuddy cannot be reached at
all, still shows the plates — an unreachable farm manager must not take the
work with it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import bambuddy as bambuddy_api
from ..integrations.base import IntegrationError
from ..models import JOB_FAILED, JOB_PENDING, JOB_PRINTING, JOB_QUEUED
from ..services import printing
from ..services.credentials import IntegrationNotConfigured

# Plates worth putting on a machine's card: still to happen, or gone wrong.
# A plate that finished is history, and history belongs in the list below.
OPEN_STATUSES = (JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_FAILED)


def _key(printer_id: Any) -> str | None:
    """Printer ids arrive as ints from one side and strings from the other."""
    return None if printer_id is None else str(printer_id)


# Where each machine said its camera was, last time the farm was read.
#
# Some builds put a camera URL on the printer row rather than serving an
# endpoint for it, and the frame proxy needs that URL. Taking it from the
# browser instead would make the proxy fetch whatever it was handed, which is
# not something an endpoint should offer however narrowly it is guarded. So the
# page passes a printer id and nothing else, and the URL is looked up here.
_camera_urls: dict[str, str] = {}


def remember_cameras(printers: list[dict[str, Any]]) -> None:
    _camera_urls.clear()
    for row in printers:
        key, url = _key(row.get("id")), row.get("camera_url")
        if key is not None and url:
            _camera_urls[key] = str(url)


def camera_url_for(printer_id: Any) -> str:
    return _camera_urls.get(str(printer_id), "")


RUNNING_WORDS = ("running", "printing", "busy", "prepare")
IDLE_WORDS = ("idle", "finish", "finished", "ready", "standby")


def _doing(printer: dict[str, Any]) -> str:
    """One word for what a machine is up to, from whichever field said it."""
    if printer.get("online") is False:
        return "offline"
    word = str(printer.get("state") or printer.get("status") or "").strip().lower()
    if word in RUNNING_WORDS:
        return "printing"
    if word in ("pause", "paused"):
        return "paused"
    if word in ("failed", "error"):
        return "failed"
    if word in IDLE_WORDS:
        return "idle"
    return "unknown"


def _summary(printers: list[dict[str, Any]], plates: list[dict[str, Any]]) -> dict[str, Any]:
    """The farm in one line, for the top of the page.

    What a person wants before they read any card: how much of the farm is
    working, how much of it could be, and when the machines that are busy will
    be free. The longest remaining time is the one that matters — the farm is
    clear when the last one finishes, not the first.
    """
    doing: dict[str, int] = {}
    for printer in printers:
        state = _doing(printer)
        doing[state] = doing.get(state, 0) + 1

    remaining = [
        printer["remaining_minutes"]
        for printer in printers
        if _doing(printer) == "printing"
        and isinstance(printer.get("remaining_minutes"), int)
        and printer["remaining_minutes"] > 0
    ]
    open_plates = [plate for plate in plates if plate["status"] in OPEN_STATUSES]
    return {
        "machines": len(printers),
        "by_state": doing,
        "printing": doing.get("printing", 0),
        "idle": doing.get("idle", 0),
        "offline": doing.get("offline", 0),
        # When the whole farm is free, not the first machine to finish.
        "busy_until_minutes": max(remaining) if remaining else None,
        "plates_open": len(open_plates),
        "plates_waiting": sum(1 for plate in open_plates if plate["printer_id"] is None),
        "units_open": sum(plate["units_expected"] for plate in open_plates),
    }


async def overview(session: AsyncSession) -> dict[str, Any]:
    """Every machine with its plates, plus the plates that have no machine.

    The farm read is allowed to fail on its own. A shop whose Bambuddy is down
    still needs to see what is queued and still needs the Re-queue button —
    that is when they need it most — so a failure becomes a message on the page
    rather than an error instead of the page.
    """
    plates = await printing.open_jobs_overview(session)

    printers: list[dict[str, Any]] = []
    error: str | None = None
    detailed = False
    detail_error: str | None = None
    try:
        client = await bambuddy_api.client_for(session)
        found = await bambuddy_api.read_farm(session, client)
        printers = found["printers"]
        detailed = found["detailed"]
        detail_error = found["detail_error"]
        remember_cameras(printers)
    except IntegrationNotConfigured as exc:
        error = str(exc)
    except IntegrationError as exc:
        error = str(exc)

    by_printer: dict[str | None, list[dict[str, Any]]] = {}
    for plate in plates:
        if plate["status"] not in OPEN_STATUSES:
            continue
        by_printer.setdefault(_key(plate["printer_id"]), []).append(plate)

    known = {_key(row.get("id")) for row in printers}
    cards = [
        {**row, "plates": by_printer.get(_key(row.get("id")), [])} for row in printers
    ]

    # Plates with no machine, and plates on a machine the farm did not list —
    # a printer that was removed from Bambuddy after the plate went to it. Both
    # need a person, and both would vanish if only the cards were drawn.
    homeless: list[dict[str, Any]] = []
    for printer_key, group in by_printer.items():
        if printer_key is None or printer_key not in known:
            homeless.extend(group)
    homeless.sort(key=lambda plate: plate["created_at"], reverse=True)

    return {
        "printers": cards,
        "summary": _summary(cards, plates),
        "unplaced": homeless,
        "plates": plates,
        "error": error,
        # Whether Bambuddy said anything about what the machines are doing, and
        # why not where it did not. A farm of cards with no readings on them is
        # a build that keeps them somewhere PrintFlow has not been pointed at,
        # which is a different problem from an idle shop.
        "live": detailed,
        "detail_error": detail_error,
    }
