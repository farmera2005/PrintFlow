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
from ..services import controls, printing
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


def doing(printer: dict[str, Any]) -> str:
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


async def state_of(session: AsyncSession, client: Any, printer_id: Any) -> str:
    """What one machine is doing right now, however this build reports it.

    For the moment before a control is sent, which is not the same moment the
    card was drawn. Two calls at worst and usually one: most builds put the live
    readings on the farm listing, and only a build whose listing is a bare
    inventory of names and models needs the machine asked directly.

    Anything that goes wrong on the way is `unknown`, which allows the control
    through. A status PrintFlow could not read is not evidence against what the
    operator can see with their own eyes.
    """
    try:
        rows = await bambuddy_api.with_healing(
            session, client, ("printers",), client.list_printers
        )
    except IntegrationError:
        return "unknown"
    row = next((r for r in rows if _key(r.get("id")) == _key(printer_id)), None)
    if row is None:
        return "unknown"
    state = doing(row)
    if state == "unknown":
        # The listing did not say what this machine is doing — some builds keep
        # that on the machine's own endpoint. One extra call, for one machine,
        # only when the cheap answer was no answer.
        try:
            extra = await bambuddy_api.with_healing(
                session,
                client,
                ("printer_detail",),
                lambda: client.read_printer(printer_id),
            )
        except IntegrationError:
            return "unknown"
        return doing({**row, **{k: v for k, v in extra.items() if v is not None}})
    return state


def _summary(printers: list[dict[str, Any]], plates: list[dict[str, Any]]) -> dict[str, Any]:
    """The farm in one line, for the top of the page.

    What a person wants before they read any card: how much of the farm is
    working, how much of it could be, and when the machines that are busy will
    be free. The longest remaining time is the one that matters — the farm is
    clear when the last one finishes, not the first.
    """
    counts: dict[str, int] = {}
    for printer in printers:
        state = doing(printer)
        counts[state] = counts.get(state, 0) + 1

    remaining = [
        printer["remaining_minutes"]
        for printer in printers
        if doing(printer) == "printing"
        and isinstance(printer.get("remaining_minutes"), int)
        and printer["remaining_minutes"] > 0
    ]
    open_plates = [plate for plate in plates if plate["status"] in OPEN_STATUSES]
    return {
        "machines": len(printers),
        "by_state": counts,
        "printing": counts.get("printing", 0),
        "idle": counts.get("idle", 0),
        "offline": counts.get("offline", 0),
        # When the whole farm is free, not the first machine to finish.
        "busy_until_minutes": max(remaining) if remaining else None,
        "plates_open": len(open_plates),
        "plates_waiting": sum(1 for plate in open_plates if plate["printer_id"] is None),
        "units_open": sum(plate["units_expected"] for plate in open_plates),
    }


# Queue statuses that mean "still to happen". A finished item is history and
# clutters the line somebody is trying to read.
QUEUE_OPEN = ("", "pending", "waiting", "queued", "scheduled", "idle", "sent",
              "printing", "running", "active", "started", "in_progress", "prepare")


def _queue_line(
    items: list[dict[str, Any]], plates: list[dict[str, Any]]
) -> dict[str | None, list[dict[str, Any]]]:
    """Bambuddy's queue, split by machine and told what each item is for.

    The queue is the machine's own line, not PrintFlow's: it holds the plates
    dispatched from orders *and* whatever anybody sent from Bambuddy's own
    screen or from Print a file. Showing only the half PrintFlow put there
    would describe a machine as free while it works through six jobs.

    So every item is drawn, and the ones PrintFlow recognises are labelled with
    the order they belong to. An item with no label is not a mystery — it is
    somebody's test piece, and saying so is the useful part.
    """
    by_queue_id = {
        str(plate["bambuddy_queue_id"]): plate
        for plate in plates
        if plate.get("bambuddy_queue_id") is not None
    }

    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for position, item in enumerate(items):
        state = str(item.get("status") or "").strip().lower()
        if state not in QUEUE_OPEN:
            continue
        plate = by_queue_id.get(str(item.get("id")))
        grouped.setdefault(_key(item.get("printer_id")), []).append(
            {
                **item,
                # Where the build stated an order, honour it; otherwise the
                # order the rows arrived in is the only answer there is, and
                # it is the order Bambuddy will work through them.
                "position": item.get("position") if item.get("position") is not None
                else position,
                "order_number": plate["order_number"] if plate else None,
                "product_name": plate["product_name"] if plate else None,
                "print_job_id": plate["id"] if plate else None,
                # Whether this is one of ours at all. The alternative to saying
                # so is a queue where half the rows look like they lost their
                # order, rather than never having had one.
                "from_order": plate is not None,
            }
        )
    for line in grouped.values():
        line.sort(key=lambda row: row["position"])
    return grouped


async def read_queue(
    session: AsyncSession, client: Any, plates: list[dict[str, Any]]
) -> tuple[dict[str | None, list[dict[str, Any]]], str | None]:
    """The farm's queue, grouped by machine. One call, not one per machine.

    A queue that cannot be read is a message rather than an error: the cards
    are still worth drawing, and a machine's temperature is not less true
    because its queue endpoint moved.
    """
    try:
        items = await bambuddy_api.with_healing(
            session, client, ("queue",), client.list_queue
        )
    except IntegrationError as exc:
        return {}, str(exc)
    return _queue_line(items, plates), None


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
    queues: dict[str | None, list[dict[str, Any]]] = {}
    queue_error: str | None = None
    offered: dict[str, str] = {}
    try:
        client = await bambuddy_api.client_for(session)
        found = await bambuddy_api.read_farm(session, client)
        printers = found["printers"]
        detailed = found["detailed"]
        detail_error = found["detail_error"]
        remember_cameras(printers)
        queues, queue_error = await read_queue(session, client, plates)
        # What this instance can be told to do to a machine. Asked once and
        # then remembered, so this is free on all but the first load.
        try:
            offered = await bambuddy_api.ensure_controls(session, client)
        except IntegrationError:
            # No buttons, and the rest of the page is unaffected. A farm that
            # cannot be commanded can still be read.
            offered = {}
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
        {
            **row,
            "plates": by_printer.get(_key(row.get("id")), []),
            # What this machine will actually work through, in the order it
            # will do it — Bambuddy's line, not PrintFlow's list.
            "queue": queues.get(_key(row.get("id")), []),
            # Pause, Resume, Stop and whatever else this build offers, each
            # already told whether it applies to what this machine is doing.
            "controls": controls.for_state(
                offered,
                doing(row),
                printer=str(row.get("name") or "this printer"),
            ),
        }
        for row in printers
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
        # Queued jobs on a machine the farm did not list, or on none at all.
        # Bambuddy dispatches an item with no printer_id to whichever machine
        # comes free, so this is a real state rather than an error.
        "queue_unassigned": queues.get(None, []),
        "queue_error": queue_error,
    }
