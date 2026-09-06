"""ShipStation client (read/write).

ShipStation imports Etsy orders itself through its native store connection and
pushes tracking back to Etsy — this platform only matches orders and buys
labels, and never duplicates the tracking write (§4.4).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    PROVIDER_SHIPSTATION,
    TRACK_ACCEPTED,
    TRACK_DELIVERED,
    TRACK_EXCEPTION,
    TRACK_IN_TRANSIT,
    TRACK_UNKNOWN,
)
from ..services import credentials
from .base import IntegrationError, RateLimiter, new_client, request

API_BASE = "https://ssapi.shipstation.com"

# Where a parcel's progress lives. ShipStation's original API — the one that
# imports orders and buys labels, and the one this key/secret is for — has no
# opinion about whether anything arrived. Only the newer API answers that, on a
# different host, with a different credential from the same account. So it is a
# second base URL rather than a second integration: it is the same ShipStation,
# and an operator who has not pasted the tracking key has not misconfigured
# anything, they have simply not turned delivery detection on.
TRACKING_API_BASE = "https://api.shipstation.com"

# ShipStation allows 40 requests/minute; stay a touch under to leave headroom
# for a concurrently running poll.
_limiter = RateLimiter(max_calls=38, period=60.0)


class ShipStationClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.api_key = str(payload.get("api_key") or "")
        self.api_secret = str(payload.get("api_secret") or "")
        if not (self.api_key and self.api_secret):
            raise IntegrationError(PROVIDER_SHIPSTATION, "ShipStation API key/secret missing")
        self.store_id = payload.get("store_id")
        # Optional, and its absence is not an error: see TRACKING_API_BASE.
        self.tracking_key = str(payload.get("tracking_api_key") or "").strip()

    @property
    def can_track(self) -> bool:
        return bool(self.tracking_key)

    async def _tracking_call(self, path: str, **params: Any) -> Any:
        if not self.can_track:
            raise IntegrationError(
                PROVIDER_SHIPSTATION,
                "No ShipStation tracking API key — delivery detection is off.",
            )
        await _limiter.acquire()
        async with new_client(base_url=TRACKING_API_BASE) as client:
            response = await request(
                client,
                "GET",
                path,
                provider=PROVIDER_SHIPSTATION,
                headers={"API-Key": self.tracking_key, "Accept": "application/json"},
                params={k: v for k, v in params.items() if v is not None} or None,
            )
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise IntegrationError(
                PROVIDER_SHIPSTATION, f"ShipStation returned non-JSON from {path}"
            ) from exc

    async def track(self, *, carrier_code: str, tracking_number: str) -> dict[str, Any]:
        """What the carrier says about one parcel, as ShipStation relays it."""
        found = await self._tracking_call(
            "/v2/tracking", carrier_code=carrier_code, tracking_number=tracking_number
        )
        return found if isinstance(found, dict) else {}

    async def tracking_carriers(self) -> list[dict[str, Any]]:
        """The carriers this key can see — and, incidentally, whether it works.

        The right question to ask a credential is one that needs nothing else
        to be true. Asking about a *parcel* needs a carrier code and a tracking
        number, so a refusal could mean the key is wrong, or that this account
        has no such carrier, or that no such parcel exists. This asks the key
        about itself.
        """
        data = await self._tracking_call("/v2/carriers")
        rows = data if isinstance(data, list) else (data or {}).get("carriers") or []
        return [row for row in rows if isinstance(row, dict)]

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.api_key}:{self.api_secret}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        await _limiter.acquire()
        async with new_client(base_url=API_BASE) as client:
            response = await request(
                client,
                method,
                path,
                provider=PROVIDER_SHIPSTATION,
                headers=self._headers(),
                **kwargs,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                PROVIDER_SHIPSTATION, f"ShipStation returned non-JSON from {path}"
            ) from exc

    async def list_stores(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/stores", params={"showInactive": "false"})
        rows = data if isinstance(data, list) else data.get("stores") or []
        return [
            {
                "store_id": row.get("storeId"),
                "store_name": row.get("storeName"),
                "marketplace": row.get("marketplaceName"),
                "active": row.get("active", True),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    async def find_order_by_number(self, order_number: str) -> dict[str, Any] | None:
        data = await self._call("GET", "/orders", params={"orderNumber": order_number})
        orders = data.get("orders") if isinstance(data, dict) else None
        for order in orders or []:
            if str(order.get("orderNumber")) == str(order_number):
                return order
        return (orders or [None])[0]

    async def get_order(self, order_id: int) -> dict[str, Any]:
        return await self._call("GET", f"/orders/{order_id}")

    async def list_carriers(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/carriers")
        return data if isinstance(data, list) else data.get("carriers") or []

    async def list_services(self, carrier_code: str) -> list[dict[str, Any]]:
        data = await self._call(
            "GET", "/carriers/listservices", params={"carrierCode": carrier_code}
        )
        return data if isinstance(data, list) else data.get("services") or []

    async def list_packages(self, carrier_code: str) -> list[dict[str, Any]]:
        data = await self._call(
            "GET", "/carriers/listpackages", params={"carrierCode": carrier_code}
        )
        return data if isinstance(data, list) else data.get("packages") or []

    async def list_warehouses(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/warehouses")
        rows = data if isinstance(data, list) else data.get("warehouses") or []
        return [row for row in rows if isinstance(row, dict)]

    async def get_rates(
        self,
        *,
        carrier_code: str,
        from_postal_code: str,
        to_state: str | None,
        to_country: str,
        to_postal_code: str,
        weight: dict[str, Any],
        service_code: str | None = None,
        package_code: str | None = None,
        confirmation: str | None = None,
        residential: bool | None = None,
    ) -> list[dict[str, Any]]:
        """What this carrier would charge. A quote, not a purchase.

        `serviceCode` is left out deliberately where the caller has not fixed
        one: ShipStation then prices every service the carrier offers, which is
        one request instead of one per line of a dropdown.
        """
        body: dict[str, Any] = {
            "carrierCode": carrier_code,
            "fromPostalCode": from_postal_code,
            "toCountry": to_country or "US",
            "toPostalCode": to_postal_code,
            "weight": weight,
        }
        if to_state:
            body["toState"] = to_state
        if service_code:
            body["serviceCode"] = service_code
        if package_code:
            body["packageCode"] = package_code
        if confirmation:
            body["confirmation"] = confirmation
        if residential is not None:
            body["residential"] = residential
        data = await self._call("POST", "/shipments/getrates", json=body, retries=1)
        rows = data if isinstance(data, list) else (data or {}).get("rates") or []
        return [row for row in rows if isinstance(row, dict)]

    async def create_label_for_order(
        self,
        *,
        order_id: int,
        carrier_code: str,
        service_code: str,
        package_code: str,
        weight: dict[str, Any],
        confirmation: str | None = None,
        ship_date: str | None = None,
        warehouse_id: Any = None,
        test_label: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "orderId": order_id,
            "carrierCode": carrier_code,
            "serviceCode": service_code,
            "packageCode": package_code,
            "weight": weight,
            "testLabel": test_label,
        }
        if confirmation:
            body["confirmation"] = confirmation
        if ship_date:
            body["shipDate"] = ship_date
        # Where it ships from. This endpoint has no `shipFrom` — an address is
        # only accepted by `shipments/createlabel`, which produces a label that
        # is not attached to the order, and ShipStation pushes tracking back to
        # the sales channel *per order*. So the origin is named the way this
        # endpoint understands it: by warehouse, through advancedOptions.
        #
        # Omitted entirely when nothing was chosen, rather than sent as null —
        # that is what leaves ShipStation on the order's own warehouse, which
        # is the behaviour every existing label was bought with.
        if warehouse_id not in (None, ""):
            body["advancedOptions"] = {"warehouseId": _as_int(warehouse_id)}
        # Labels cost money: never retry a create automatically. A duplicate
        # would be a duplicate purchase.
        return await self._call(
            "POST", "/orders/createlabelfororder", json=body, retries=0
        )


def order_defaults(order: dict[str, Any]) -> dict[str, Any]:
    """Pre-populate the label dialog from ShipStation's own defaults for the order."""
    weight = order.get("weight") or {}
    return {
        "carrier_code": order.get("carrierCode"),
        "service_code": order.get("serviceCode"),
        "package_code": order.get("packageCode") or "package",
        "confirmation": order.get("confirmation"),
        "weight_value": weight.get("value"),
        "weight_units": weight.get("units") or "ounces",
        "order_status": order.get("orderStatus"),
        "ship_to": order.get("shipTo") or {},
    }


def parse_rate(row: dict[str, Any]) -> dict[str, Any]:
    """One quoted service. `otherCost` is surcharges, and it is charged too."""
    shipment = row.get("shipmentCost")
    other = row.get("otherCost")
    total: Decimal | None = None
    for part in (shipment, other):
        if part is None:
            continue
        try:
            total = (total or Decimal(0)) + Decimal(str(part))
        except (ArithmeticError, ValueError):
            continue
    return {
        "service_code": row.get("serviceCode"),
        "service_name": row.get("serviceName"),
        "total": total,
    }


def _as_int(value: Any) -> Any:
    """ShipStation wants warehouseId as a number, and a form gives us a string.

    Left alone when it is not a number at all: sending it back as it arrived
    lets ShipStation say what is wrong with it, which beats a coercion error
    from here naming a field the operator never saw.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def rank_warehouses(
    warehouses: list[dict[str, Any]], warehouse_id: Any
) -> list[dict[str, Any]]:
    """The ship-from locations, most wanted first.

    One ordering used by everything that needs an origin, so the address a
    quote is priced from cannot disagree with the one a label ships from.
    """
    rows = [row for row in warehouses if isinstance(row, dict)]
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("warehouseId")) != str(warehouse_id),
            not row.get("isDefault"),
        ),
    )


def origin_postal_code(warehouses: list[dict[str, Any]], warehouse_id: Any) -> str | None:
    """Where the parcel ships from — the one thing a quote needs that an order
    does not carry. The named warehouse if there is one, else the default,
    else whichever came first: any of them prices better than none of them."""
    for row in rank_warehouses(warehouses, warehouse_id):
        address = row.get("originAddress") or {}
        code = address.get("postalCode") if isinstance(address, dict) else None
        if code:
            return str(code)
    return None


def origin_warehouse(
    warehouses: list[dict[str, Any]], warehouse_id: Any
) -> dict[str, Any] | None:
    """The whole warehouse a parcel would ship from, not just its postcode.

    The label needs the id and the operator needs the address; resolving both
    from one ranking is what stops the screen naming one place and the carrier
    collecting from another.
    """
    for row in rank_warehouses(warehouses, warehouse_id):
        if row.get("warehouseId") is not None:
            return row
    return None


def warehouse_summary(row: dict[str, Any]) -> dict[str, Any]:
    """One ship-from location, in the words a person picks it by.

    A warehouse id is a number nobody recognises, so the address goes with it
    everywhere — choosing the wrong origin is a parcel collected from the wrong
    building, and it is only noticed when it does not turn up.
    """
    address = row.get("originAddress") or {}
    if not isinstance(address, dict):
        address = {}
    parts = [
        address.get("street1"),
        address.get("city"),
        " ".join(
            str(bit).strip()
            for bit in (address.get("state"), address.get("postalCode"))
            if str(bit or "").strip()
        ),
    ]
    where = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    name = str(row.get("warehouseName") or "").strip()
    return {
        "warehouse_id": row.get("warehouseId"),
        "name": name or None,
        "address": where or None,
        # What the picker shows on one line: the name people call it, then
        # enough address to tell two of them apart.
        "label": " — ".join(bit for bit in (name, where) if bit) or str(
            row.get("warehouseId")
        ),
        "postal_code": str(address.get("postalCode") or "").strip() or None,
        "is_default": bool(row.get("isDefault")),
    }


def label_cost(response: dict[str, Any]) -> Decimal | None:
    """What the label actually cost, postage and insurance together.

    Both parts are charged and both appear on the shipping bill, so a card
    showing only the postage would be quietly wrong on any order insured. A
    reply that prices nothing gives nothing back rather than zero: "ShipStation
    did not say" and "it was free" are different, and only one of them should
    ever be shown as $0.00.
    """
    total = Decimal(0)
    seen = False
    for field in ("shipmentCost", "insuranceCost"):
        value = response.get(field)
        if value is None:
            continue
        try:
            total += Decimal(str(value))
        except (ArithmeticError, ValueError):
            continue
        seen = True
    return total if seen else None


def decode_label_pdf(response: dict[str, Any]) -> bytes | None:
    raw = response.get("labelData")
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError):
        return None


# ShipStation's standardised codes, which every carrier's own vocabulary is
# mapped onto before it reaches us. Only the delivered one changes what
# PrintFlow does; the rest are shown so an operator can see a parcel moving.
TRACK_CODES = {
    "UN": TRACK_UNKNOWN,      # not in the carrier's system yet
    "NY": TRACK_UNKNOWN,      # accepted by ShipStation, not yet scanned
    "AC": TRACK_ACCEPTED,
    "IT": TRACK_IN_TRANSIT,
    "AT": TRACK_IN_TRANSIT,   # attempted delivery — still out there
    "DE": TRACK_DELIVERED,
    "EX": TRACK_EXCEPTION,
}

# The same answer spelled out, for a build or a carrier that sends words rather
# than a code. Checked as whole words so "not delivered" cannot read as
# delivered — the one mistake in here that would move a card wrongly.
TRACK_WORDS = {
    "delivered": TRACK_DELIVERED,
    "delivery": TRACK_DELIVERED,
    "in_transit": TRACK_IN_TRANSIT,
    "in transit": TRACK_IN_TRANSIT,
    "transit": TRACK_IN_TRANSIT,
    "accepted": TRACK_ACCEPTED,
    "exception": TRACK_EXCEPTION,
    "error": TRACK_EXCEPTION,
    "unknown": TRACK_UNKNOWN,
}


def _parse_when(value: Any) -> datetime | None:
    """A carrier timestamp, always as an aware UTC datetime.

    Carriers send local time with an offset, local time without one, and
    occasionally a trailing Z. A naive one is read as UTC rather than dropped:
    the hour may be an hour out, and "delivered, time uncertain" beats "not
    delivered".
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def parse_tracking(payload: dict[str, Any]) -> dict[str, Any]:
    """One parcel's progress, reduced to what the board acts on.

    The status code is trusted over the sentence beside it, because the code is
    the part ShipStation standardises across carriers and the sentence is
    whatever the carrier's own system wrote. Only where there is no code does
    the wording get a say — and then only on an exact word, since a carrier
    that says "not delivered" must not move a card into Complete.
    """
    if not isinstance(payload, dict):
        return {"status": TRACK_UNKNOWN, "delivered_at": None, "detail": None, "url": None}

    code = str(payload.get("status_code") or payload.get("statusCode") or "").strip()
    status = TRACK_CODES.get(code.upper()) if code else None
    if status is None:
        spelled = str(
            payload.get("status_description")
            or payload.get("statusDescription")
            or ""
        ).strip().lower()
        status = TRACK_WORDS.get(spelled, TRACK_UNKNOWN)

    delivered_at = _parse_when(
        payload.get("actual_delivery_date") or payload.get("actualDeliveryDate")
    )
    # A carrier that says delivered without saying when still delivered it. The
    # caller stamps the time it found out, which is close enough to run a
    # 48-hour board clock from and honest about where it came from.
    detail = (
        payload.get("carrier_status_description")
        or payload.get("carrierStatusDescription")
        or payload.get("status_description")
        or payload.get("statusDescription")
    )
    events = payload.get("events")
    if not detail and isinstance(events, list) and events:
        last = events[-1]
        if isinstance(last, dict):
            detail = last.get("description") or last.get("event_description")
    return {
        "status": status,
        "delivered_at": delivered_at,
        "detail": str(detail).strip() if detail else None,
        "url": str(payload.get("tracking_url") or payload.get("trackingUrl") or "")
        or None,
        "estimated_delivery": _parse_when(
            payload.get("estimated_delivery_date")
            or payload.get("estimatedDeliveryDate")
        ),
    }


async def client_for(session: AsyncSession) -> ShipStationClient:
    payload = await credentials.require(session, PROVIDER_SHIPSTATION)
    return ShipStationClient(payload)
