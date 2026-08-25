"""Wix eCommerce client: the orders a Wix site has taken.

**An API key rather than OAuth.** Wix offers both, and OAuth is the right answer
for an app listed in Wix's own marketplace and installed on other people's
sites. PrintFlow is neither: it is one shop's software, on one shop's machine,
reading one shop's orders. An API key is made in the Wix dashboard in a minute,
carries no callback URL, and cannot expire out from under a poll at three in the
morning — which matters here, where the Etsy connection's need for a public
callback is the single fiddliest part of setting the whole thing up.

The key is account-wide, so a site id says which of the account's sites this is.
Both are sent as headers on every call.

**Read-only.** Nothing here writes to Wix. Orders come in, and everything that
happens to them afterwards happens in PrintFlow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PROVIDER_WIX
from ..services import credentials
from .base import IntegrationError, RateLimiter, new_client, request

log = logging.getLogger("printflow.wix")

API_BASE = "https://www.wixapis.com"

# Wix's published ceiling is generous and per-endpoint; this is well under it
# and exists so a first sync of a busy shop cannot look like an attack.
_limiter = RateLimiter(max_calls=100, period=60.0)

# How many orders one page of the search asks for. Wix caps this at 100.
PAGE_SIZE = 100

# What a person pressing "Save & validate" waits behind.
#
# The background poll can afford the default patience — three retries at a 30s
# read timeout is over two minutes, which costs nothing when no one is watching
# and saves a poll cycle from a transient blip. An interactive check cannot:
# PrintFlow sits behind a reverse proxy, Cloudflare gives up at 100s and serves
# its own 502 page, and the operator is then told PrintFlow is broken when the
# truth was "Wix never answered".
#
# So the interactive path answers once, quickly. Retrying a credential check is
# close to pointless anyway — a key that is wrong is still wrong three attempts
# later, and the only thing retrying buys is a transient 5xx, which the person
# can retry themselves by pressing the button again.
INTERACTIVE_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)
INTERACTIVE_RETRIES = 0


class WixClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.api_key = str(payload.get("api_key") or "").strip()
        self.site_id = str(payload.get("site_id") or "").strip()
        if not self.api_key:
            raise IntegrationError(PROVIDER_WIX, "No Wix API key is configured.")
        if not self.site_id:
            raise IntegrationError(PROVIDER_WIX, "No Wix site ID is configured.")
        self.payload = payload

    def _headers(self) -> dict[str, str]:
        # The key goes in Authorization without a scheme — Wix API keys are not
        # bearer tokens and it rejects them when they are labelled as such.
        return {
            "Authorization": self.api_key,
            "wix-site-id": self.site_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _call(
        self,
        method: str,
        path: str,
        *,
        retries: int | None = None,
        timeout: httpx.Timeout | None = None,
        **kwargs: Any,
    ) -> Any:
        await _limiter.acquire()
        client_args: dict[str, Any] = {"base_url": API_BASE}
        if timeout is not None:
            client_args["timeout"] = timeout
        async with new_client(**client_args) as client:
            try:
                response = await request(
                    client,
                    method,
                    path,
                    provider=PROVIDER_WIX,
                    headers=self._headers(),
                    **({} if retries is None else {"retries": retries}),
                    **kwargs,
                )
            except IntegrationError as exc:
                raise _explain(exc) from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                PROVIDER_WIX, f"Wix returned something that was not JSON from {path}"
            ) from exc

    async def validate(self) -> dict[str, Any]:
        """Prove the key and the site id go together, and say what was found.

        One page of one order rather than a dedicated ping: it exercises
        exactly the call the poll makes, so a key that reads orders is a key
        that has been shown to read orders.

        Bounded, because somebody is watching a spinner and a proxy is watching
        the clock — see INTERACTIVE_TIMEOUT.
        """
        found = await self.search_orders(
            limit=1, retries=INTERACTIVE_RETRIES, timeout=INTERACTIVE_TIMEOUT
        )
        orders = found.get("orders") or []
        return {
            "site_id": self.site_id,
            "orders_visible": bool(orders),
            "newest": parse_order(orders[0]) if orders else None,
        }

    async def search_orders(
        self,
        *,
        since: str | None = None,
        cursor: str | None = None,
        limit: int = PAGE_SIZE,
        retries: int | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        """One page of orders, newest first.

        A cursor and a filter cannot be sent together — Wix carries the
        original query inside the cursor — so a continuation sends only the
        cursor, which is why this takes both and uses one.

        `retries` and `timeout` are how the interactive check borrows this call
        without borrowing the background poll's patience.
        """
        if cursor:
            body: dict[str, Any] = {"search": {"cursorPaging": {"cursor": cursor}}}
        else:
            search: dict[str, Any] = {
                "cursorPaging": {"limit": max(1, min(limit, PAGE_SIZE))},
                "sort": [{"fieldName": "createdDate", "order": "DESC"}],
            }
            if since:
                search["filter"] = {"createdDate": {"$gte": since}}
            body = {"search": search}
        return await self._call(
            "POST",
            "/ecom/v1/orders/search",
            json=body,
            retries=retries,
            timeout=timeout,
        )

    async def iter_orders(
        self, *, since: str | None = None, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        """Every order since a moment, oldest first.

        Paged with a stop, like every other listing in this codebase: a shop
        connecting an established site should not have its first poll walk
        years of history in one request that eventually times out. Reversed on
        the way out so intake sees them in the order they were placed.
        """
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = await self.search_orders(since=since, cursor=cursor)
            orders = page.get("orders") or []
            collected.extend(row for row in orders if isinstance(row, dict))
            cursor = ((page.get("metadata") or {}).get("cursors") or {}).get("next")
            if not cursor or not orders:
                break
        collected.reverse()
        return collected


def _explain(exc: IntegrationError) -> IntegrationError:
    """Turn Wix's status codes into something an operator can act on.

    A 401 and a 403 are different mistakes with different fixes, and neither is
    obvious from the number — the second one in particular usually means the
    key is fine and the *permissions* on it are not.
    """
    hints = {
        401: (
            "Wix would not accept that API key. Check it was copied whole from "
            "Settings → API Keys in the Wix dashboard."
        ),
        403: (
            "That API key works but is not allowed to read this site's orders. "
            "In the Wix dashboard, give the key the Wix Stores or eCommerce "
            "read permissions, and check the site ID is one the key covers."
        ),
        404: (
            "Wix has no site with that ID. It is the one in the site's "
            "dashboard URL, not the site's address."
        ),
        428: (
            "The Wix site has no eCommerce app installed, so it has no orders "
            "to read."
        ),
    }
    hint = hints.get(exc.status_code or 0)
    if not hint:
        return exc
    # `type(exc)` rather than IntegrationError: adding the hint must not quietly
    # downgrade the class. A 401 arrives as AuthExpiredError, and rebuilding it
    # as a plain IntegrationError loses the one fact worth keeping — that these
    # are *credentials* being refused, not the service being broken. Everything
    # that branches on that, from the route's status code to the app-wide
    # "reconnect it in Settings" handler, stops seeing Wix.
    return type(exc)(
        PROVIDER_WIX,
        f"{exc.args[0] if exc.args else 'Wix refused the request'}. {hint}",
        status_code=exc.status_code,
        body=exc.body,
    )


# --------------------------------------------------------------------------
# Reading what Wix sends
# --------------------------------------------------------------------------


def _money(raw: Any) -> Decimal | None:
    """A figure out of a Wix price object, or a bare string.

    Wix states money as `{"amount": "18.00", "formattedAmount": "$18.00"}`. The
    amount is a string on purpose and is kept as one all the way to Decimal:
    routing it through a float is how 18.10 becomes 18.099999999999998 in
    somebody's books.
    """
    if isinstance(raw, dict):
        raw = raw.get("amount")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _placed_at(order: dict[str, Any]) -> datetime | None:
    raw = order.get("createdDate") or order.get("purchasedDate")
    if not raw:
        return None
    try:
        # Wix sends RFC 3339 with a Z; fromisoformat wants an offset it knows.
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def buyer_name(order: dict[str, Any]) -> str | None:
    """Who to put on the despatch note.

    The recipient first and the buyer second: a gift is bought by one person
    and sent to another, and the name that matters to a shop packing a box is
    the one going on the label.
    """
    for holder in ("recipientInfo", "billingInfo", "buyerInfo"):
        contact = (order.get(holder) or {}).get("contactDetails") or {}
        parts = [contact.get("firstName"), contact.get("lastName")]
        name = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        if name:
            return name
    email = (order.get("buyerInfo") or {}).get("email")
    return str(email) if email else None


def ship_to(order: dict[str, Any]) -> dict[str, Any] | None:
    """Where it is going, in the shape the rest of PrintFlow already uses."""
    info = order.get("recipientInfo") or order.get("billingInfo") or {}
    address = info.get("address") or {}
    contact = info.get("contactDetails") or {}
    parts = {
        "name": buyer_name(order),
        "first_line": address.get("addressLine") or address.get("addressLine1"),
        "second_line": address.get("addressLine2"),
        "city": address.get("city"),
        "state": address.get("subdivision") or address.get("subdivisionFullname"),
        "zip": address.get("postalCode"),
        "country": address.get("country"),
        "email": (order.get("buyerInfo") or {}).get("email"),
        "phone": contact.get("phone"),
    }
    kept = {k: str(v).strip() for k, v in parts.items() if isinstance(v, str) and v.strip()}
    return kept or None


def totals(order: dict[str, Any]) -> dict[str, Any]:
    """What the buyer paid, split the way PrintFlow splits it.

    The same keys the Etsy reader produces, so the order columns, the Money
    tab and the invoice do not care which channel filled them in.
    """
    summary = order.get("priceSummary") or {}
    return {
        "currency": str(order.get("currency") or "").strip() or None,
        "revenue": _money(summary.get("total")),
        "items_total": _money(summary.get("subtotal")),
        "shipping_total": _money(summary.get("shipping")),
        "tax_total": _money(summary.get("tax")),
        # Wix states a discount as a positive amount taken off, which is how
        # PrintFlow stores it too.
        "discount_total": _money(summary.get("discount")),
    }


def options_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """What the buyer chose, normalised to PrintFlow's {name, value} pairs.

    Wix puts them in two places depending on how the catalogue was built —
    `catalogReference.options.options` for a Stores product, and
    `descriptionLines` for anything with custom text — and a shop can use both
    on one item. Both are read, because either can be the one that changes what
    gets made.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(name: Any, value: Any) -> None:
        label = str(name or "").strip()
        chosen = str(value or "").strip()
        if not label or not chosen or label.lower() in seen:
            return
        seen.add(label.lower())
        out.append({"name": label, "value": chosen})

    chosen = ((item.get("catalogReference") or {}).get("options") or {}).get("options")
    if isinstance(chosen, dict):
        for name, value in chosen.items():
            add(name, value)

    for line in item.get("descriptionLines") or []:
        if not isinstance(line, dict):
            continue
        name = (line.get("name") or {}).get("original")
        value = (line.get("plainText") or {}).get("original")
        if value is None:
            value = (line.get("colorInfo") or {}).get("original")
        add(name, value)
    return out


def parse_line(item: dict[str, Any]) -> dict[str, Any]:
    """One line item, in PrintFlow's words."""
    catalogue = item.get("catalogReference") or {}
    physical = item.get("physicalProperties") or {}
    quantity = item.get("quantity")
    try:
        quantity = max(1, int(quantity))
    except (TypeError, ValueError):
        quantity = 1
    return {
        "wix_line_item_id": str(item.get("id") or "") or None,
        "wix_catalog_item_id": str(catalogue.get("catalogItemId") or "") or None,
        # The exact combination bought, where the catalogue names one.
        "wix_variant_id": str(
            ((catalogue.get("options") or {}).get("variantId")) or ""
        ) or None,
        "sku": str(physical.get("sku") or "").strip() or None,
        "title": (item.get("productName") or {}).get("original"),
        "quantity": quantity,
        # Per unit, not per line: Wix states the quantity beside it, the same
        # way Etsy does, so this is the number an invoice line wants.
        "unit_price": _money(item.get("price")),
        "options": options_of(item),
    }


def parse_order(order: dict[str, Any]) -> dict[str, Any]:
    """A Wix order in PrintFlow's words. Pure, so intake is testable."""
    return {
        "wix_order_id": str(order.get("id") or "") or None,
        # Wix's own human-readable number, which is what a shop and a buyer
        # both call the order. Falls back to the id when a site has none.
        "number": str(order.get("number") or order.get("id") or "").strip() or None,
        "placed_at": _placed_at(order),
        "buyer_name": buyer_name(order),
        "ship_to": ship_to(order),
        "status": str(order.get("status") or "").strip() or None,
        "archived": bool(order.get("archived")),
        "lines": [
            parse_line(item)
            for item in (order.get("lineItems") or [])
            if isinstance(item, dict)
        ],
        **totals(order),
    }


async def client_for(session: AsyncSession) -> WixClient:
    payload = await credentials.require(session, PROVIDER_WIX)
    return WixClient(payload)
