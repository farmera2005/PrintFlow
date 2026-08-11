"""ShipStation client (read/write).

ShipStation imports Etsy orders itself through its native store connection and
pushes tracking back to Etsy — this platform only matches orders and buys
labels, and never duplicates the tracking write (§4.4).
"""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PROVIDER_SHIPSTATION
from ..services import credentials
from .base import IntegrationError, RateLimiter, new_client, request

API_BASE = "https://ssapi.shipstation.com"

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


async def client_for(session: AsyncSession) -> ShipStationClient:
    payload = await credentials.require(session, PROVIDER_SHIPSTATION)
    return ShipStationClient(payload)
