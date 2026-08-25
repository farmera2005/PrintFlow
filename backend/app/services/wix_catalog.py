"""What Wix sells, lined up against what PrintFlow knows how to make.

The Etsy equivalent of this lives in `catalog.py` and is kept separate on
purpose: the two channels have different identifiers, different link tables and
different failure modes, and the one thing worse than two similar modules is
one module with a channel flag threaded through every function.

**Why matching works at all.** A shop running both channels usually built the
Wix catalogue by importing it from Etsy, so each Wix item has an exact Etsy
counterpart — same title, same product code. That is what makes bulk linking
possible: PrintFlow does not have to guess what a Wix item is, it has to
recognise something it already knows under a second name.

So the ladder below is ordered by how much a match is worth trusting:

1. **the product code** — the same comparison intake uses. If Wix and PrintFlow
   agree on a SKU, that is the end of the question.
2. **the Etsy listing's title** — for the shops the link table exists for, who
   never filled a SKU in anywhere. The Wix item was imported from that listing
   and carries its title, and PrintFlow stored that title when the Etsy link
   was made. Matching them is transitive: this Wix item is that Etsy listing,
   and that Etsy listing is this product.
3. **the product's own name** — last, and weakest, because a product's name is
   ours to edit and drifts from what either channel calls it.

Nothing here links anything on its own. Every rule produces a *proposal* the
operator confirms, because a wrong link sends real orders to the wrong product
and is only noticed when the wrong thing comes off a printer.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EtsyProductLink, Product, WixProductLink

log = logging.getLogger("printflow.wix_catalog")

# How a row was matched, in the order they are tried. On screen next to each
# proposal, because "matched on the code" and "matched on a title" deserve
# different amounts of trust from whoever is about to confirm it.
BY_SKU = "sku"
BY_ETSY_TITLE = "etsy_title"
BY_PRODUCT_NAME = "product_name"

# Already linked, so there is nothing to propose.
LINKED = "linked"
# Nothing recognised it.
MISSING = "missing"

MATCH_LABELS = {
    BY_SKU: "product code",
    BY_ETSY_TITLE: "its Etsy listing's title",
    BY_PRODUCT_NAME: "the product name",
}


def normalize(raw: str | None) -> str:
    """The same comparison intake uses, so this screen cannot disagree with it."""
    return (raw or "").strip().casefold()


class Index:
    """Everything PrintFlow knows, keyed the three ways a Wix item can be recognised.

    Built once per reconcile rather than queried per item: a catalogue of a few
    hundred items against a few hundred products is two queries this way and
    several hundred the obvious way.
    """

    def __init__(
        self,
        products: list[Product],
        etsy_links: list[EtsyProductLink],
        wix_links: list[WixProductLink],
    ) -> None:
        self.by_id = {product.id: product for product in products}
        self.by_sku: dict[str, Product] = {}
        self.by_name: dict[str, Product] = {}
        for product in products:
            if key := normalize(product.sku):
                self.by_sku.setdefault(key, product)
            if key := normalize(product.name):
                self.by_name.setdefault(key, product)

        # An Etsy listing title -> the product that listing resolves to. Only
        # unambiguous titles are kept: two listings sharing a title and pointing
        # at different products is exactly the case where a confident guess
        # would be wrong, so the title is dropped and the row falls through.
        titles: dict[str, set[Any]] = {}
        for link in etsy_links:
            key = normalize(link.listing_title)
            if key:
                titles.setdefault(key, set()).add(link.product_id)
        self.by_etsy_title = {
            title: owners.pop()
            for title, owners in titles.items()
            if len(owners) == 1
        }

        self.linked_items = {
            link.wix_catalog_item_id
            for link in wix_links
            if link.wix_variant_id is None
        }
        self.linked_by_item = {
            link.wix_catalog_item_id: link
            for link in wix_links
            if link.wix_variant_id is None
        }

    def match(self, item: dict[str, Any]) -> tuple[Product | None, str | None]:
        """Recognise one Wix item, most trustworthy rule first."""
        if key := normalize(item.get("sku")):
            if found := self.by_sku.get(key):
                return found, BY_SKU
        if key := normalize(item.get("name")):
            if owner := self.by_etsy_title.get(key):
                if found := self.by_id.get(owner):
                    return found, BY_ETSY_TITLE
            if found := self.by_name.get(key):
                return found, BY_PRODUCT_NAME
        return None, None


async def build_index(session: AsyncSession) -> Index:
    products = (await session.execute(select(Product))).scalars().all()
    etsy_links = (await session.execute(select(EtsyProductLink))).scalars().all()
    wix_links = (await session.execute(select(WixProductLink))).scalars().all()
    return Index(list(products), list(etsy_links), list(wix_links))


async def reconcile(session: AsyncSession, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Line up every Wix catalogue item against the product table."""
    index = await build_index(session)

    rows: list[dict[str, Any]] = []
    for item in items:
        item_id = item.get("wix_catalog_item_id")
        if not item_id:
            continue

        existing = index.linked_by_item.get(item_id)
        if existing is not None:
            product = index.by_id.get(existing.product_id)
            rows.append(
                _row(item, product, LINKED, link_id=existing.id)
            )
            continue

        product, how = index.match(item)
        rows.append(_row(item, product, how or MISSING))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "items": rows,
        "counts": counts,
        # What could be linked in one press, so the button can say a number
        # rather than the operator having to count the rows themselves.
        "proposed": sum(1 for row in rows if row["product_id"] and row["status"] != LINKED),
        "total": len(rows),
    }


def _row(
    item: dict[str, Any], product: Product | None, status: str, *, link_id: Any = None
) -> dict[str, Any]:
    return {
        "wix_catalog_item_id": item.get("wix_catalog_item_id"),
        "name": item.get("name"),
        "sku": item.get("sku"),
        "visible": item.get("visible", True),
        "variant_count": len(item.get("variants") or []),
        "status": status,
        "matched_on": MATCH_LABELS.get(status),
        "product_id": product.id if product else None,
        "product_sku": product.sku if product else None,
        "product_name": product.name if product else None,
        "link_id": link_id,
    }


async def link_items(
    session: AsyncSession,
    items: list[dict[str, Any]],
    item_ids: list[str],
) -> dict[str, Any]:
    """Create the links the operator confirmed. Returns what happened to each.

    Re-matched here rather than trusting ids sent from the browser: the
    proposal the operator saw was computed from a catalogue read minutes ago,
    and a product renamed in between must not cause a link nobody agreed to.
    """
    index = await build_index(session)
    by_id = {
        item.get("wix_catalog_item_id"): item
        for item in items
        if item.get("wix_catalog_item_id")
    }

    linked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item_id in item_ids:
        item = by_id.get(item_id)
        if item is None:
            skipped.append({"wix_catalog_item_id": item_id, "reason": "Wix no longer sells it"})
            continue
        if item_id in index.linked_items:
            skipped.append({"wix_catalog_item_id": item_id, "reason": "already linked"})
            continue
        product, how = index.match(item)
        if product is None:
            skipped.append(
                {"wix_catalog_item_id": item_id, "reason": "nothing matches it"}
            )
            continue

        session.add(
            WixProductLink(
                product_id=product.id,
                wix_catalog_item_id=item_id,
                item_title=item.get("name"),
            )
        )
        await session.flush()
        index.linked_items.add(item_id)
        linked.append(
            {
                "wix_catalog_item_id": item_id,
                "product_id": product.id,
                "product_sku": product.sku,
                "matched_on": MATCH_LABELS.get(how or ""),
            }
        )

    return {"linked": linked, "skipped": skipped}
