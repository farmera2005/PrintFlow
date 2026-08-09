"""Short handles for products, generated when the shop has none of its own.

Every product needs something short to print next to it — on a card, in a
QuickBooks description, in an error message. That used to be the Etsy SKU, which
worked right up until it turned out Etsy never required one and this shop had
never filled any in.

So the handle is now generated, and generated from the Etsy identity where there
is one: ``ETSY-1895497697`` is the listing the product came from, and can be
pasted straight into an Etsy URL. It is a label, not a key — matching is done on
the Etsy ids themselves (see :mod:`app.services.intake`), and nothing goes
looking for a product by this string unless the operator typed one in by hand.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Product

MAX_LENGTH = 200


def from_listing(listing_id: int | None, etsy_product_id: int | None = None) -> str | None:
    """``ETSY-<listing>``, or ``ETSY-<listing>-<variation>`` for one variation."""
    if not listing_id:
        return None
    if etsy_product_id:
        return f"ETSY-{listing_id}-{etsy_product_id}"
    return f"ETSY-{listing_id}"


def from_name(name: str | None) -> str:
    """A last resort for a product typed in by hand with no code and no listing."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
    return slug[:60] or "PRODUCT"


async def unique(session: AsyncSession, base: str) -> str:
    """`base`, or `base-2`, `base-3`… — whichever is free.

    Codes are compared without case, the same way the unique index does it, so a
    suggestion cannot collide with an existing code that differs only in case.
    """
    base = base.strip()[:MAX_LENGTH] or "PRODUCT"
    taken = {
        value.casefold()
        for value in (
            await session.execute(
                select(Product.sku).where(
                    func.lower(Product.sku).like(f"{base.lower()}%")
                )
            )
        )
        .scalars()
        .all()
    }
    if base.casefold() not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate.casefold() not in taken:
            return candidate
    # A thousand products sharing one listing is not a thing that happens, but
    # returning the bare base and letting the unique index reject it is a
    # clearer failure than looping forever.
    return base
