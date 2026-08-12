"""Where the parcel is, and when the card is finished with.

An order that has shipped is not done — it is in a van, and the thing the shop
actually wants to know is whether it arrived. Nobody wants to sit on the
Shipped column reading tracking numbers into a carrier's website, so PrintFlow
asks on their behalf and moves the card when the answer comes back.

Three separable jobs live here, and only the middle one needs a carrier:

* a link, so a tracking number on a card goes where a tracking number should;
* the poll, which asks ShipStation what each parcel is doing;
* the sweep, which takes a delivered card off the board two days later.

The first and last work whether or not delivery detection is switched on, which
matters: a shop with no tracking key still gets clickable numbers and still
gets a board that empties itself once somebody drags a card to Complete.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import shipstation as ss_api
from ..integrations.base import IntegrationError
from ..models import (
    COMPLETE_BOARD_HOURS,
    ORDER_COMPLETE,
    ORDER_SHIPPED,
    TRACK_DELIVERED,
    Order,
)
from ..services import audit, credentials

log = logging.getLogger("printflow.tracking")


# --------------------------------------------------------------------------
# A tracking number that goes somewhere
# --------------------------------------------------------------------------

# Where each carrier puts its own tracking page. `{}` is the number.
#
# Keyed on the codes ShipStation uses, which are not the carrier's name: a
# label bought through Stamps.com or Endicia is a USPS parcel and USPS is who
# can say where it is. Anything not listed gets no link rather than a guess —
# a link to the wrong carrier's "not found" page is worse than a number the
# operator can paste themselves, because it looks like an answer.
CARRIER_URLS: dict[str, str] = {
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "stamps_com": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "endicia": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "ups": "https://www.ups.com/track?tracknum={}",
    "ups_walleted": "https://www.ups.com/track?tracknum={}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={}",
    "dhl_express": "https://www.dhl.com/en/express/tracking.html?AWB={}",
    "dhl_ecommerce": "https://webtrack.dhlecs.com/?trackingnumber={}",
    "dhl_global_mail": "https://webtrack.dhlecs.com/?trackingnumber={}",
    "ontrac": "https://www.ontrac.com/tracking/?number={}",
    "canada_post": "https://www.canadapost-postescanada.ca/track-reperage/en#/resultList?searchFor={}",
    "royal_mail": "https://www.royalmail.com/track-your-item#/tracking-results/{}",
    "australia_post": "https://auspost.com.au/mypost/track/#/details/{}",
    "globalpost": "https://track.goglobalpost.com/{}",
    "asendia": "https://tracking.asendiausa.com/?trackingNumber={}",
}


def tracking_url(
    carrier_code: str | None, number: str | None, stored: str | None = None
) -> str | None:
    """Where to send somebody who clicks the tracking number.

    The carrier's own link, where ShipStation gave one, because it is the one
    that will still work when a carrier reorganises its site. Otherwise the
    known page for that carrier. Otherwise nothing.
    """
    if stored and str(stored).strip().lower().startswith(("http://", "https://")):
        return str(stored).strip()
    number = str(number or "").strip()
    if not number:
        return None
    template = CARRIER_URLS.get(str(carrier_code or "").strip().lower())
    return template.format(number) if template else None


# --------------------------------------------------------------------------
# Asking the carrier
# --------------------------------------------------------------------------

# How long to leave a parcel alone between checks, by how many times it has
# been asked about. A parcel does not move on a fifteen-second timer: the first
# scan takes hours and the middle of a journey is quiet for a day at a time. So
# the gap widens, then settles at half a day, which is the point at which the
# question stops being "has it arrived" and starts being "is it lost".
BACKOFF_HOURS = (0, 2, 4, 8, 12)

# When to stop asking. Thirty days of a parcel that never arrives is not a
# parcel any more, and the card stays in Shipped where somebody will see it.
MAX_TRACKING_ATTEMPTS = 90


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def next_check_due(
    attempts: int, checked_at: datetime | None, now: datetime | None = None
) -> bool:
    if attempts >= MAX_TRACKING_ATTEMPTS:
        return False
    if checked_at is None or attempts == 0:
        return True
    wait = BACKOFF_HOURS[min(attempts, len(BACKOFF_HOURS) - 1)]
    now = now or datetime.now(timezone.utc)
    return now - _aware(checked_at) >= timedelta(hours=wait)


async def mark_complete(
    session: AsyncSession,
    order: Order,
    *,
    delivered_at: datetime | None = None,
    actor: str = "tracking",
    note: str | None = None,
) -> None:
    """Move a card into Complete and start its 48-hour clock.

    The one status change PrintFlow makes on its own, so it says so in the
    audit log and in the note on the card: an operator who finds an order
    somewhere they did not put it is owed an explanation on the card itself.
    """
    was = order.status
    order.status = ORDER_COMPLETE
    order.delivered_at = delivered_at or order.delivered_at
    order.completed_at = datetime.now(timezone.utc)
    if note:
        order.status_note = note
    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="delivered",
        detail={
            "from": was,
            "to": ORDER_COMPLETE,
            "delivered_at": (delivered_at or order.delivered_at).isoformat()
            if (delivered_at or order.delivered_at)
            else None,
            "tracking_number": order.tracking_number,
            "carrier_code": order.carrier_code,
        },
        actor=actor,
    )


async def poll_deliveries(session: AsyncSession, *, limit: int = 40) -> dict[str, Any]:
    """Ask about every shipped parcel that is due a check, and move the arrivals.

    Only orders sitting in Shipped with a tracking number: one that has been
    dragged elsewhere is somebody's decision and not this job's business, and
    one with no number cannot be asked about.
    """
    stats = {"checked": 0, "delivered": 0, "moving": 0, "failed": 0, "skipped": None}

    client = await ss_api.client_for(session)
    if not client.can_track:
        stats["skipped"] = "no tracking key"
        return stats

    candidates = (
        (
            await session.execute(
                select(Order)
                .where(
                    Order.status == ORDER_SHIPPED,
                    Order.tracking_number.isnot(None),
                    Order.carrier_code.isnot(None),
                )
                .order_by(Order.label_created_at.desc().nullslast())
                .limit(limit * 4)
            )
        )
        .scalars()
        .all()
    )
    due = [
        order
        for order in candidates
        if next_check_due(order.tracking_attempts, order.tracking_checked_at)
    ][:limit]
    if not due:
        stats["skipped"] = "nothing due"
        return stats

    now = datetime.now(timezone.utc)
    for order in due:
        order.tracking_attempts += 1
        order.tracking_checked_at = now
        stats["checked"] += 1
        try:
            payload = await client.track(
                carrier_code=order.carrier_code, tracking_number=order.tracking_number
            )
        except IntegrationError as exc:
            # One parcel the carrier will not discuss must not stop the rest —
            # a tracking number typed wrong would otherwise block the whole
            # farm's deliveries for as long as it sat there.
            log.warning("Tracking failed for %s: %s", order.order_number, exc)
            stats["failed"] += 1
            continue

        found = ss_api.parse_tracking(payload)
        order.tracking_status = found["status"]
        order.tracking_detail = found["detail"]
        order.tracking_status_at = now
        if found["url"]:
            order.tracking_url = found["url"]

        if found["status"] == TRACK_DELIVERED:
            await mark_complete(
                session,
                order,
                delivered_at=found["delivered_at"] or now,
                note=found["detail"] or "Carrier reported delivery.",
            )
            stats["delivered"] += 1
        else:
            stats["moving"] += 1

    await session.flush()
    if stats["checked"] and stats["failed"] < stats["checked"]:
        await credentials.mark_ok(session, "shipstation")
    return stats


# --------------------------------------------------------------------------
# Taking finished cards off the board
# --------------------------------------------------------------------------


def board_cutoff(now: datetime | None = None) -> datetime:
    """The moment before which a Complete card is no longer drawn."""
    return (now or datetime.now(timezone.utc)) - timedelta(hours=COMPLETE_BOARD_HOURS)


def still_on_board(order: Order, now: datetime | None = None) -> bool:
    """Whether the board should draw this card.

    Everything that is not Complete, always. A Complete card for its first two
    days. And a Complete card with no `completed_at` — an order that reached
    the column before this existed — because a card that has been on the board
    for months is not something to make vanish on the strength of a null.
    """
    if order.status != ORDER_COMPLETE:
        return True
    if order.completed_at is None:
        return True
    return _aware(order.completed_at) > board_cutoff(now)
