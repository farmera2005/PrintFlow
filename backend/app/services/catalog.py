"""What Etsy sells, checked against what PrintFlow knows how to make.

Order intake matches a receipt's SKU to a product, and a SKU that does not
match leaves the line marked unmatched — visible, but only once a real order has
already arrived and stalled. This reads the shop's listings directly so the
comparison can be made up front, while there is time to fix it.

The reconciliation is deliberately three-sided:

* **matched** — Etsy sells it, PrintFlow can make it.
* **missing** — Etsy sells it, PrintFlow has never heard of it. These are the
  orders that will stall.
* **unused** — PrintFlow has a product no live listing sells under that SKU.
  Usually a component, sometimes a typo, occasionally a listing that has been
  retired and forgotten.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import etsy as etsy_api
from ..integrations.base import IntegrationError
from ..models import Product

log = logging.getLogger("printflow.catalog")

# Enough for any shop this is built for, and a hard stop on the per-listing
# inventory fallback so a large shop cannot quietly spend a thousand requests.
MAX_INVENTORY_FETCHES = 120


def normalize_sku(raw: str | None) -> str:
    """The same comparison intake uses, so this screen cannot disagree with it."""
    return (raw or "").strip().casefold()


async def fetch_listings(
    client: etsy_api.EtsyClient, *, state: str = "active"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Listings with their inventory, however Etsy is willing to give it.

    Returns (listings, notes). Notes describe how the data was actually
    obtained — whether inventory came inline or had to be fetched per listing,
    and whether the fetch was cut short — because a comparison built from
    partial data must not look complete.
    """
    if not client.shop_id:
        raise IntegrationError("etsy", "No Etsy shop selected")

    listings = await client.iter_listings(shop_id=client.shop_id, state=state)
    notes: list[str] = []

    missing = [
        listing
        for listing in listings
        if etsy_api.listing_inventory_of(listing) is None
    ]
    if missing:
        fetched = 0
        for listing in missing:
            if fetched >= MAX_INVENTORY_FETCHES:
                notes.append(
                    f"Stopped after reading variations for {MAX_INVENTORY_FETCHES} "
                    f"listings; {len(missing) - fetched} were left. Their options are "
                    "not shown below."
                )
                break
            listing_id = listing.get("listing_id")
            if listing_id is None:
                continue
            try:
                listing["inventory"] = await client.listing_inventory(listing_id)
            except IntegrationError as exc:
                log.info("No inventory for listing %s: %s", listing_id, exc)
                continue
            fetched += 1
        if fetched:
            notes.append(
                f"Etsy did not include variations with the listings, so they were "
                f"read one listing at a time ({fetched} of them)."
            )
    return listings, notes


async def reconcile(
    session: AsyncSession, listings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Line up every SKU Etsy sells against the product table."""
    products = (await session.execute(select(Product))).scalars().all()
    by_sku = {normalize_sku(p.sku): p for p in products}
    seen_skus: set[str] = set()

    rows: list[dict[str, Any]] = []
    for listing in listings:
        inventory = etsy_api.listing_inventory_of(listing)
        variants = etsy_api.listing_variants(listing, inventory)
        options = etsy_api.listing_options(variants)
        for variant in variants:
            key = normalize_sku(variant.get("sku"))
            product = by_sku.get(key) if key else None
            if key:
                seen_skus.add(key)
            rows.append(
                {
                    "listing_id": listing.get("listing_id"),
                    "title": listing.get("title"),
                    "state": listing.get("state"),
                    "url": listing.get("url"),
                    "sku": variant.get("sku"),
                    "options": variant.get("options") or [],
                    "listing_options": options,
                    "product_id": product.id if product else None,
                    "product_sku": product.sku if product else None,
                    "product_name": product.name if product else None,
                    "fulfillment": product.fulfillment if product else None,
                    "qbo_item_id": product.qbo_item_id if product else None,
                    "status": _status(variant.get("sku"), product),
                }
            )

    unused = [
        {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "fulfillment": product.fulfillment,
        }
        for key, product in by_sku.items()
        if key not in seen_skus
    ]
    unused.sort(key=lambda row: row["sku"].lower())

    counts = {
        "listings": len({row["listing_id"] for row in rows if row["listing_id"]}),
        "variants": len(rows),
        "matched": sum(1 for row in rows if row["status"] == "matched"),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "no_sku": sum(1 for row in rows if row["status"] == "no_sku"),
        "unused_products": len(unused),
    }
    return {"rows": rows, "unused_products": unused, "counts": counts}


def _status(sku: str | None, product: Product | None) -> str:
    if not (sku or "").strip():
        # An Etsy listing with no SKU cannot be matched at all: intake has
        # nothing to look up, and the order will land unmatched.
        return "no_sku"
    return "matched" if product else "missing"
