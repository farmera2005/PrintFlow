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

# How many catalogue items one page asks for. Wix caps this at 100.
CATALOG_PAGE_SIZE = 100


class WixClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.api_key = str(payload.get("api_key") or "").strip()
        self.site_id = str(payload.get("site_id") or "").strip()
        if not self.api_key:
            raise IntegrationError(PROVIDER_WIX, "No Wix API key is configured.")
        if not self.site_id:
            raise IntegrationError(PROVIDER_WIX, "No Wix site ID is configured.")
        self.payload = payload
        # Which Stores generation this site answered on, once we know. Saves
        # re-probing a 404 on every page of a long catalogue walk.
        self._catalog_api: str | None = None

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

    # ----------------------------------------------------------------------
    # The catalogue
    # ----------------------------------------------------------------------

    async def catalog_page(
        self, *, offset: int = 0, limit: int = CATALOG_PAGE_SIZE
    ) -> tuple[list[dict[str, Any]], int, str]:
        """One page of catalogue items, whichever Stores API this site answers on.

        Wix Stores has two live generations and a given site may have either.
        Rather than pick one and be wrong on half the installs, this tries each
        in turn and remembers which answered — the same shape of problem as the
        Bambuddy endpoint discovery, solved the same way.

        Returns (items, total, api_version).
        """
        attempts = CATALOG_APIS if self._catalog_api is None else [
            api for api in CATALOG_APIS if api["version"] == self._catalog_api
        ]
        last: IntegrationError | None = None
        for api in attempts:
            try:
                payload = await self._call(
                    "POST", api["path"], json=api["body"](offset, limit)
                )
            except IntegrationError as exc:
                # A 404 means this site does not speak that generation; anything
                # else — a rejected key, no permissions — is the real answer and
                # trying the other version would only bury it.
                if exc.status_code != 404:
                    raise
                last = exc
                continue
            self._catalog_api = api["version"]
            items = [
                parse_catalog_item(row)
                for row in (payload.get("products") or [])
                if isinstance(row, dict)
            ]
            return items, api["total"](payload, len(items)), api["version"]
        # Every generation 404'd. Deliberately not re-raising the last one:
        # `_explain` reads a 404 as "no site with that id", which is true of the
        # orders endpoint — the site id is the only thing there that can miss —
        # and quite wrong here, where it means this site does not speak that
        # generation. Sending somebody to re-check a site id that is correct is
        # worse than saying nothing.
        raise IntegrationError(
            PROVIDER_WIX,
            "This Wix site has no Stores catalogue to read. The API key needs "
            "the Wix Stores read permission, and the site needs Wix Stores "
            "installed.",
            status_code=404,
            body=last.body if last else None,
        )

    async def iter_catalog(self, *, max_pages: int = 20) -> tuple[list[dict[str, Any]], str]:
        """The whole catalogue, paged with a stop. Returns (items, api_version)."""
        collected: list[dict[str, Any]] = []
        version = ""
        for page in range(max_pages):
            items, total, version = await self.catalog_page(
                offset=page * CATALOG_PAGE_SIZE
            )
            collected.extend(items)
            if not items or len(collected) >= total:
                break
        return collected, version


def _v1_body(offset: int, limit: int) -> dict[str, Any]:
    return {"query": {"paging": {"offset": offset, "limit": limit}}}


def _v3_body(offset: int, limit: int) -> dict[str, Any]:
    return {"search": {"cursorPaging": {"offset": offset, "limit": limit}}}


# The two Stores generations, newest first. `total` says how many items exist,
# so paging knows when to stop; both report it in a different place and neither
# is guaranteed to report it at all, hence the fallback to "as many as we got".
CATALOG_APIS: list[dict[str, Any]] = [
    {
        "version": "v3",
        "path": "/stores/v3/products/search",
        "body": _v3_body,
        "total": lambda payload, got: int(
            (payload.get("pagingMetadata") or {}).get("total") or got
        ),
    },
    {
        "version": "v1",
        "path": "/stores-reader/v1/products/query",
        "body": _v1_body,
        "total": lambda payload, got: int(payload.get("totalResults") or got),
    },
]


def parse_catalog_item(row: dict[str, Any]) -> dict[str, Any]:
    """One catalogue item, in PrintFlow's words, from either generation.

    The id here is the one an order carries as `catalogItemId`, which is what
    makes a link from this screen match an order later.

    A SKU can live in three places depending on generation and on whether the
    item has variants at all, so all three are read and the first real one
    wins. Getting this wrong would show a whole catalogue as having no SKUs,
    and the SKU is the entire basis of matching.
    """
    variants = _catalog_variants(row)
    sku = (
        _clean(row.get("sku"))
        # v3 puts a single-variant item's code on the variant, not the product.
        or next((variant["sku"] for variant in variants if variant["sku"]), None)
    )
    return {
        "wix_catalog_item_id": _clean(row.get("id")),
        "name": _name_of(row),
        "sku": sku,
        "visible": row.get("visible", row.get("visibility", True)) is not False,
        "variants": variants,
    }


def _name_of(row: dict[str, Any]) -> str | None:
    """The item's title. A bare string on one generation, a translated object
    on the other — `_clean` on the object would stringify the dict itself."""
    raw = row.get("name")
    if isinstance(raw, dict):
        return _clean(raw.get("original") or raw.get("translated"))
    return _clean(raw)


def _catalog_variants(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Buyable combinations, flattened out of whichever shape arrived."""
    raw = row.get("variants")
    if not isinstance(raw, list):
        raw = ((row.get("variantsInfo") or {}).get("variants")) or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # v1 nests the sellable detail under "variant"; v3 has it flat.
        detail = entry.get("variant") if isinstance(entry.get("variant"), dict) else entry
        out.append(
            {
                "wix_variant_id": _clean(entry.get("id")),
                "sku": _clean(detail.get("sku")),
                "choices": _choices(entry),
            }
        )
    return out


def _choices(entry: dict[str, Any]) -> dict[str, str]:
    """What this combination is, as {option: value}."""
    raw = entry.get("choices")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k and v}
    out: dict[str, str] = {}
    for choice in raw if isinstance(raw, list) else []:
        if not isinstance(choice, dict):
            continue
        name = _clean(choice.get("optionName") or choice.get("name"))
        value = _clean(choice.get("choiceName") or choice.get("value"))
        if name and value:
            out[name] = value
    return out


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
