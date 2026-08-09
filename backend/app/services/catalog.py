"""What Etsy sells, checked against what PrintFlow knows how to make.

Order intake matches a receipt's SKU to a product, and a SKU that does not
match leaves the line marked unmatched — visible, but only once a real order has
already arrived and stalled. This reads the shop's listings directly so the
comparison can be made up front, while there is time to fix it.

The reconciliation is deliberately three-sided:

* **matched** — Etsy sells it, PrintFlow can make it.
* **linked** — no SKU on Etsy, but the listing has been pointed at a product by
  hand. It matches; it just does not match on a SKU.
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
from ..models import EtsyProductLink, Product
from ..services import codes

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
    by_id = {product.id: product for product in products}
    seen_skus: set[str] = set()

    # The other way a listing reaches a product: named by hand, for listings
    # that carry no SKU at all.
    links = (await session.execute(select(EtsyProductLink))).scalars().all()
    by_variant = {
        (link.etsy_listing_id, link.etsy_product_id): link
        for link in links
        if link.etsy_product_id is not None
    }
    by_listing = {
        link.etsy_listing_id: link for link in links if link.etsy_product_id is None
    }

    rows: list[dict[str, Any]] = []
    for listing in listings:
        listing_id = listing.get("listing_id")
        inventory = etsy_api.listing_inventory_of(listing)
        variants = etsy_api.listing_variants(listing, inventory)
        options = etsy_api.listing_options(variants)
        for variant in variants:
            key = normalize_sku(variant.get("sku"))
            product = by_sku.get(key) if key else None
            if key:
                seen_skus.add(key)

            linked = False
            if product is None and listing_id is not None:
                # Same order of preference intake uses: the exact variant first,
                # then the listing as a whole.
                link = by_variant.get(
                    (listing_id, variant.get("product_id"))
                ) or by_listing.get(listing_id)
                if link is not None:
                    product = by_id.get(link.product_id)
                    linked = product is not None

            rows.append(
                {
                    "listing_id": listing_id,
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
                    "status": _status(variant.get("sku"), product, linked),
                }
            )

    # "Nothing on Etsy sells this" has to account for both ways a listing can
    # reach a product. A linked product is sold; its code was never meant to
    # appear in Etsy, so looking for it there would report every one of them.
    sold_by_link = {link.product_id for link in links}
    unused = [
        {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "fulfillment": product.fulfillment,
        }
        for key, product in by_sku.items()
        if key not in seen_skus and product.id not in sold_by_link
    ]
    unused.sort(key=lambda row: row["sku"].lower())

    counts = {
        "listings": len({row["listing_id"] for row in rows if row["listing_id"]}),
        "variants": len(rows),
        "matched": sum(1 for row in rows if row["status"] == "matched"),
        "linked": sum(1 for row in rows if row["status"] == "linked"),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "no_sku": sum(1 for row in rows if row["status"] == "no_sku"),
        "unused_products": len(unused),
    }
    return {"rows": rows, "unused_products": unused, "counts": counts}


async def import_listings(
    session: AsyncSession,
    listings: list[dict[str, Any]],
    listing_ids: list[int],
    *,
    fulfillment: str,
) -> dict[str, Any]:
    """Create a product per Etsy listing, already linked to it.

    This is the path that makes a shop with no SKUs workable: a listing carries
    its own identity, so PrintFlow can build the product from it and record the
    link in the same step, without anyone inventing a code.

    One product per listing, not per variation. Etsy issues a fresh product id
    for a variation every time the listing's options are edited, so a
    per-variation product would stop matching after the next edit; what differs
    between variations belongs in BOM option rules, which match on the values.
    """
    wanted = set(listing_ids)
    by_id = {
        listing.get("listing_id"): listing
        for listing in listings
        if listing.get("listing_id") in wanted
    }

    linked_already = set(
        (
            await session.execute(
                select(EtsyProductLink.etsy_listing_id).where(
                    EtsyProductLink.etsy_listing_id.in_(wanted)
                )
            )
        )
        .scalars()
        .all()
    )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for listing_id in listing_ids:
        listing = by_id.get(listing_id)
        if listing is None:
            skipped.append({"listing_id": listing_id, "reason": "Etsy no longer lists it"})
            continue
        if listing_id in linked_already:
            skipped.append({"listing_id": listing_id, "reason": "already linked"})
            continue

        title = (listing.get("title") or "").strip() or f"Etsy listing {listing_id}"
        # If the listing does happen to carry one SKU, keep it as the code — it
        # is the shop's own word for the thing, and better than a generated one.
        # Matching does not depend on it either way; the link does that.
        variants = etsy_api.listing_variants(
            listing, etsy_api.listing_inventory_of(listing)
        )
        skus = {
            (variant.get("sku") or "").strip()
            for variant in variants
            if (variant.get("sku") or "").strip()
        }
        base = skus.pop() if len(skus) == 1 else codes.from_listing(listing_id) or ""

        product = Product(
            sku=await codes.unique(session, base),
            name=title[:500],
            fulfillment=fulfillment,
            qbo_item_id=None,
            active=True,
        )
        session.add(product)
        await session.flush()
        session.add(
            EtsyProductLink(
                product_id=product.id,
                etsy_listing_id=listing_id,
                listing_title=title,
            )
        )
        await session.flush()
        linked_already.add(listing_id)
        created.append(
            {"listing_id": listing_id, "product_id": product.id, "code": product.sku}
        )

    return {"created": created, "skipped": skipped}


def _status(sku: str | None, product: Product | None, linked: bool) -> str:
    if linked:
        # Reached through an Etsy link rather than a SKU. Worth saying so: the
        # link is a local decision, and if the listing is ever given a real SKU
        # the SKU takes over.
        return "linked"
    if not (sku or "").strip():
        # No SKU and no link: intake has nothing to look up, and the order will
        # land unmatched.
        return "no_sku"
    return "matched" if product else "missing"
