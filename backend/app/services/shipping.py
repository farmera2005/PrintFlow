"""ShipStation order matching and label creation (§4.4)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import shipstation as ss_api
from ..integrations.base import IntegrationError
from ..models import (
    LINE_CANCELLED,
    ORDER_CANCELLED,
    ORDER_SHIPPED,
    SOURCE_MANUAL,
    Order,
    OrderLine,
)
from ..services import credentials
from ..services.manufacturing import money
from ..services.state import recompute_order

log = logging.getLogger("printflow.shipping")

# ShipStation's Etsy import can lag by up to an hour, so back off rather than
# hammering: minutes to wait before attempt N.
BACKOFF_MINUTES = (0, 5, 10, 20, 30, 45, 60)
MAX_MATCH_ATTEMPTS = 48


def next_attempt_due(
    attempts: int, last_attempt_at: datetime | None, now: datetime | None = None
) -> bool:
    if attempts >= MAX_MATCH_ATTEMPTS:
        return False
    if last_attempt_at is None or attempts == 0:
        return True
    wait = BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES) - 1)]
    now = now or datetime.now(timezone.utc)
    reference = last_attempt_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return now - reference >= timedelta(minutes=wait)


class LabelError(RuntimeError):
    pass


# What ShipStation cannot make a label without, and the words to ask for it in.
REQUIRED_ADDRESS = (
    ("name", "a name"),
    ("first_line", "a street address"),
    ("city", "a city"),
    ("zip", "a postcode"),
    ("country", "a country"),
)


def address_problem(order: Order) -> str | None:
    """Why this order cannot be shipped yet, in a sentence, or None.

    Asked before ShipStation is called rather than after: "postalCode is
    required" coming back from an API is a worse version of the same news, and
    it costs a round trip to find out.
    """
    to = order.ship_to or {}
    missing = [
        what for field, what in REQUIRED_ADDRESS if not str(to.get(field) or "").strip()
    ]
    if missing:
        return (
            "This order needs "
            + ", ".join(missing[:-1] + [f"and {missing[-1]}"] if len(missing) > 1 else missing)
            + " before ShipStation can ship it. Open Ship to and fill it in."
        )
    country = str(to.get("country") or "").strip()
    if len(country) != 2 or not country.isalpha():
        return (
            f"ShipStation wants a two-letter country code, and this order says "
            f"“{country}”. US, GB, CA and so on."
        )
    if country.upper() == "US" and not str(to.get("state") or "").strip():
        return "A US address needs a state before ShipStation can ship it."
    return None


def shipstation_body(order: Order, lines: list[OrderLine]) -> dict[str, Any]:
    """The ShipStation order for one PrintFlow order. Pure.

    Top-level lines only, for the same reason the invoice uses them: the buyer
    bought a bundle, and a packing slip itemising its BOM describes a different
    parcel from the one being packed.
    """
    to = order.ship_to or {}
    placed = order.placed_at or datetime.now(timezone.utc)
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=timezone.utc)
    address = {
        "name": str(to.get("name") or order.buyer_name or "").strip(),
        "street1": str(to.get("first_line") or "").strip(),
        "city": str(to.get("city") or "").strip(),
        "postalCode": str(to.get("zip") or "").strip(),
        "country": str(to.get("country") or "").strip().upper(),
    }
    if str(to.get("second_line") or "").strip():
        address["street2"] = str(to["second_line"]).strip()
    if str(to.get("state") or "").strip():
        address["state"] = str(to["state"]).strip()

    body: dict[str, Any] = {
        "orderNumber": order.order_number,
        # Stable, and the whole reason this call can be repeated: ShipStation
        # updates the order carrying this key instead of making another.
        "orderKey": str(order.id),
        "orderDate": placed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        # What every imported order arrives as, so it lands in the same queue
        # the shop already packs from.
        "orderStatus": "awaiting_shipment",
        "billTo": {"name": order.buyer_name or address["name"]},
        "shipTo": address,
        "items": [
            {
                "sku": line.sku_raw or "",
                "name": line.title or (line.sku_raw or "Item"),
                "quantity": int(line.quantity),
                "unitPrice": float(money(line.unit_price)) if line.unit_price else 0,
            }
            for line in lines
        ],
    }
    if str(to.get("email") or "").strip():
        body["customerEmail"] = str(to["email"]).strip()
    if order.revenue is not None:
        body["amountPaid"] = float(money(order.revenue))
    if order.tax_total is not None:
        body["taxAmount"] = float(money(order.tax_total))
    if order.shipping_total is not None:
        body["shippingAmount"] = float(money(order.shipping_total))
    return body


async def packable_lines(session: AsyncSession, order: Order) -> list[OrderLine]:
    return list(
        (
            await session.execute(
                select(OrderLine)
                .where(
                    OrderLine.order_id == order.id,
                    OrderLine.parent_line_id.is_(None),
                    OrderLine.state != LINE_CANCELLED,
                )
                .order_by(OrderLine.created_at)
            )
        )
        .scalars()
        .all()
    )


async def send_to_shipstation(
    session: AsyncSession, order: Order, *, resend: bool = False
) -> int:
    """Create this order in ShipStation, and remember which one it became.

    The counterpart of `match_orders` for an order no channel will ever import.
    Raises LabelError with a sentence worth showing when the address is not
    enough to ship from.

    `resend` sends an order ShipStation already has, which is how a corrected
    address gets there: the body carries the same `orderKey`, so ShipStation
    updates that order rather than making a second one.
    """
    if order.shipstation_order_id is not None and not resend:
        return order.shipstation_order_id
    problem = address_problem(order)
    if problem:
        raise LabelError(problem)

    client = await ss_api.client_for(session)
    created = await client.create_order(
        shipstation_body(order, await packable_lines(session, order))
    )
    remote_id = created.get("orderId")
    if not remote_id:
        raise LabelError(f"ShipStation did not return an order id: {created}")
    order.shipstation_order_id = int(remote_id)
    order.shipstation_attempts += 1
    order.shipstation_last_attempt_at = datetime.now(timezone.utc)
    # Banked now. The order exists in ShipStation either way, and losing which
    # one it became is what would have somebody packing the same parcel twice.
    await session.commit()
    return order.shipstation_order_id


async def match_orders(session: AsyncSession, *, limit: int = 50) -> dict[str, int]:
    """Find each unmatched order in ShipStation by its Etsy receipt id.

    Except the ones nothing is going to import: a typed-in order is *sent*
    rather than looked for, which is the same sweep doing the same job — after
    it, every order that can be shipped has a ShipStation order behind it.
    """
    candidates = (
        (
            await session.execute(
                select(Order)
                .where(
                    Order.shipstation_order_id.is_(None),
                    Order.status.notin_((ORDER_SHIPPED, ORDER_CANCELLED)),
                )
                .order_by(Order.placed_at.desc().nullslast())
                .limit(limit * 4)
            )
        )
        .scalars()
        .all()
    )
    due = [
        order
        for order in candidates
        if next_attempt_due(order.shipstation_attempts, order.shipstation_last_attempt_at)
    ][:limit]

    stats = {"checked": len(due), "matched": 0, "not_found": 0, "sent": 0, "no_address": 0}
    if not due:
        return stats

    client = await ss_api.client_for(session)
    now = datetime.now(timezone.utc)
    for order in due:
        order.shipstation_attempts += 1
        order.shipstation_last_attempt_at = now
        if order.source == SOURCE_MANUAL:
            # An order being collected or handed over has no address and needs
            # none. It is not a failure to match, so it is counted apart.
            if address_problem(order) is not None:
                stats["no_address"] += 1
                continue
            created = await client.create_order(
                shipstation_body(order, await packable_lines(session, order))
            )
            if created.get("orderId"):
                order.shipstation_order_id = int(created["orderId"])
                stats["sent"] += 1
            else:
                stats["not_found"] += 1
            continue
        try:
            found = await client.find_order_by_number(order.order_number)
        except IntegrationError as exc:
            log.warning("ShipStation lookup failed for %s: %s", order.order_number, exc)
            raise
        if found and found.get("orderId"):
            order.shipstation_order_id = int(found["orderId"])
            stats["matched"] += 1
        else:
            stats["not_found"] += 1
    await session.flush()
    return stats


KEY_SHIP_FROM = "ship_from_warehouse_id"


async def default_warehouse_id(session: AsyncSession) -> Any:
    """The shop's chosen ship-from, or None to leave it to ShipStation."""
    payload = await credentials.load(session, "shipstation")
    return payload.get(KEY_SHIP_FROM) or None


async def resolve_origin(
    session: AsyncSession,
    client: ss_api.ShipStationClient,
    remote: dict[str, Any],
    chosen: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Which location this parcel ships from, and everything it could ship from.

    One resolution order, used by the quote and by the purchase, so the price
    the operator was shown cannot have come from a different address than the
    parcel leaves from:

    1. what was picked for this label;
    2. the shop's default, set in Settings;
    3. the warehouse the order itself names in ShipStation;
    4. ShipStation's own default, then whatever exists.

    The order comes third deliberately. A shop that has chosen a ship-from has
    said something about where it packs parcels *now*, and an order imported
    weeks ago carrying an old warehouse should not quietly override that.
    """
    warehouses = await client.list_warehouses()
    wanted = chosen or await default_warehouse_id(session)
    if not wanted:
        wanted = (remote.get("advancedOptions") or {}).get("warehouseId")
    return ss_api.origin_warehouse(warehouses, wanted), warehouses


async def label_context(session: AsyncSession, order: Order) -> dict[str, Any]:
    """Everything the label dialog needs, pre-populated from ShipStation."""
    if order.shipstation_order_id is None:
        return {"available": False, "reason": "Not matched in ShipStation yet."}
    client = await ss_api.client_for(session)
    remote = await client.get_order(order.shipstation_order_id)
    defaults = ss_api.order_defaults(remote)
    carriers = await client.list_carriers()
    origin, warehouses = await resolve_origin(session, client, remote)
    return {
        "available": True,
        "shipstation_order_id": order.shipstation_order_id,
        "defaults": defaults,
        "carriers": [
            {"code": c.get("code"), "name": c.get("name")}
            for c in carriers
            if isinstance(c, dict)
        ],
        # Where it would ship from, and the alternatives. Both, because the
        # dialog has to show the address it is about to use *and* let it be
        # changed for this one parcel.
        "ship_from": ss_api.warehouse_summary(origin) if origin else None,
        "ship_from_options": [ss_api.warehouse_summary(row) for row in warehouses],
    }


async def label_rates(
    session: AsyncSession,
    order: Order,
    *,
    carrier_code: str,
    weight_value: float,
    weight_units: str = "ounces",
    package_code: str | None = None,
    confirmation: str | None = None,
    warehouse_id: Any = None,
) -> dict[str, Any]:
    """What this carrier would charge to ship this order, per service.

    A quote, not a purchase — and said to be one, because the two differ. The
    carrier prices again at the moment the label is bought, and a surcharge
    that depends on something ShipStation does not know here (a dimension, a
    residential flag it decides later) lands on the real charge and not on this
    one. A number that is nearly always right is worth far more than no number
    at all when the question is "is this the £30 service or the £9 one" — but it
    must not be presented as the price.

    Every service is priced in one request rather than one per service: the
    dropdown has a dozen lines and the operator is choosing between them.
    """
    if order.shipstation_order_id is None:
        return {"available": False, "reason": "Not matched in ShipStation yet."}
    if not carrier_code or not weight_value or float(weight_value) <= 0:
        return {"available": False, "reason": "Pick a carrier and a weight first."}

    client = await ss_api.client_for(session)
    remote = await client.get_order(order.shipstation_order_id)
    ship_to = remote.get("shipTo") or {}
    to_postal = str(ship_to.get("postalCode") or "").strip()
    if not to_postal:
        return {
            "available": False,
            "reason": "This order has no destination postcode in ShipStation.",
        }

    origin_row, _warehouses = await resolve_origin(
        session, client, remote, warehouse_id
    )
    origin = (ss_api.warehouse_summary(origin_row) or {}).get("postal_code") if origin_row else None
    if not origin:
        return {
            "available": False,
            "reason": (
                "ShipStation has no ship-from address, so it cannot price "
                "anything. Add a warehouse origin in ShipStation, then choose "
                "it under Settings → ShipStation."
            ),
        }

    rows = await client.get_rates(
        carrier_code=carrier_code,
        from_postal_code=origin,
        to_state=ship_to.get("state"),
        to_country=ship_to.get("country") or "US",
        to_postal_code=to_postal,
        weight={"value": float(weight_value), "units": weight_units},
        package_code=package_code or None,
        confirmation=confirmation or None,
        residential=ship_to.get("residential"),
    )
    rates = [ss_api.parse_rate(row) for row in rows]
    return {
        "available": True,
        "from_postal_code": origin,
        # Named, not just numbered. A quote priced from the wrong building is
        # only obvious if the screen says which building.
        "ship_from": ss_api.warehouse_summary(origin_row),
        "to_postal_code": to_postal,
        "rates": [
            {
                "service_code": rate["service_code"],
                "service_name": rate["service_name"],
                "total": str(money(rate["total"])) if rate["total"] is not None else None,
            }
            for rate in rates
            if rate["service_code"]
        ],
    }


async def create_label(
    session: AsyncSession,
    order: Order,
    *,
    carrier_code: str,
    service_code: str,
    package_code: str,
    weight_value: float,
    weight_units: str = "ounces",
    confirmation: str | None = None,
    warehouse_id: Any = None,
    test_label: bool = False,
) -> dict[str, Any]:
    """Buy a label. Always explicitly user-triggered — labels cost money (§4.4)."""
    if order.shipstation_order_id is None:
        raise LabelError("This order has not been matched in ShipStation yet.")
    if order.label_created_at is not None:
        raise LabelError("A label has already been created for this order.")
    if weight_value is None or float(weight_value) <= 0:
        raise LabelError("Enter a shipping weight greater than zero.")

    client = await ss_api.client_for(session)
    # The chosen ship-from, as an id and nothing more.
    #
    # This deliberately does NOT look the warehouse up. `create_label_for_order`
    # wants the id, and the id is what we already have — the only thing a
    # lookup added was a prettier address in the response, and it bought that
    # with a second ShipStation call on the one path in PrintFlow that spends
    # money. That call carried the ordinary retry budget (four attempts at a
    # 30s read), which is over two minutes, which is longer than the proxy in
    # front of PrintFlow will wait.
    #
    # Worse than the timeout: the lookup ran *before* the purchase, so a slow
    # one could push the whole request past the proxy's patience with the label
    # already bought — the operator sees a failure, presses again, and pays
    # twice. One call in, one call out.
    #
    # With nothing chosen, nothing is sent at all and ShipStation uses the
    # order's own warehouse, exactly as every label before this feature did.
    wanted = warehouse_id or await default_warehouse_id(session)

    response = await client.create_label_for_order(
        order_id=order.shipstation_order_id,
        carrier_code=carrier_code,
        service_code=service_code,
        package_code=package_code or "package",
        weight={"value": float(weight_value), "units": weight_units},
        confirmation=confirmation,
        warehouse_id=wanted or None,
        test_label=test_label,
    )

    tracking = response.get("trackingNumber")
    if not tracking:
        raise LabelError(f"ShipStation did not return a tracking number: {response}")

    order.tracking_number = str(tracking)
    order.carrier_code = carrier_code
    order.service_code = service_code
    order.label_created_at = datetime.now(timezone.utc)
    order.label_pdf = ss_api.decode_label_pdf(response)
    # The one thing PrintFlow spends money on. Kept on the order rather than
    # left in ShipStation, so the board can show what the card cost and a
    # question about last month's postage can be answered from the orders.
    order.label_cost = ss_api.label_cost(response)
    if order.label_cost is not None:
        order.label_currency = str(response.get("currency") or "USD")

    # Banked before anything else runs.
    #
    # The money is already spent by this line, and everything after it — the
    # roll-up, the audit entry, the router's own commit — is work that could
    # fail or be cancelled when a browser gives up on a slow request. Losing
    # this write would leave a label bought and no record of it, so the next
    # press would buy a second one: `label_created_at` is the only thing
    # standing between an operator and paying twice.
    await session.commit()

    await credentials.mark_ok(session, "shipstation")
    # Tracking flows back to Etsy through ShipStation's own store connection —
    # this platform never writes to Etsy.
    await recompute_order(session, order)
    return {
        "tracking_number": order.tracking_number,
        "carrier_code": carrier_code,
        "service_code": service_code,
        "shipment_cost": response.get("shipmentCost"),
        "label_cost": str(money(order.label_cost)) if order.label_cost is not None else None,
        "label_currency": order.label_currency,
        "has_pdf": order.label_pdf is not None,
        # Which ship-from it went out on. The id alone: the screen that asked
        # for it already holds the addresses, and fetching one here would put a
        # ShipStation round trip back on the path that spends money.
        "ship_from_warehouse_id": wanted or None,
    }
