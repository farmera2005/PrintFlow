"""Matching an order line to one of its product's variations, and what that changes.

A listing sells one product with several buyable combinations, and those
combinations are not interchangeable: *Bin Fan: Yes* is a different plate on the
printer from *Bin Fan: No*. This works out which combination an order is, from
what Etsy sent, and then answers the two questions the rest of the pipeline
asks — which file to print, and which QuickBooks item to draw down.

Matching is deliberately two-tier. Etsy's own variation id is exact, but Etsy
reissues those ids whenever a seller edits the listing's options, so a variation
matched only by id would go quiet after an edit and take the wrong plate with
it. Option values survive the edit. Both are kept: the id decides when it is
there and current, the values catch everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..models import OrderLine, PrintMapping, Product, ProductVariation


def normalize(value: Any) -> str:
    """The same comparison the BOM option rules use, so the two cannot disagree."""
    return " ".join(str(value or "").split()).casefold()


def option_key(options: Iterable[dict[str, Any]]) -> frozenset[tuple[str, str]]:
    """An order-independent, case-insensitive key for a set of chosen options.

    Free text is left out: a personalisation message is one buyer's engraving,
    never a variation the shop stocks, and including it would stop every
    personalised order from matching anything.
    """
    return frozenset(
        (normalize(option.get("name")), normalize(option.get("value")))
        for option in options or []
        if option.get("name")
        and option.get("value")
        and not option.get("free_text")
    )


def label_for(options: Iterable[dict[str, Any]]) -> str:
    """"Bin Fan: Yes · Colour: Red" — what a person calls this combination."""
    parts = [
        f"{str(option.get('name')).strip()}: {str(option.get('value')).strip()}"
        for option in options or []
        if option.get("name") and option.get("value")
    ]
    return " · ".join(parts) or "Default"


def automatch(
    line: OrderLine, variations: list[ProductVariation]
) -> ProductVariation | None:
    """The variation this line bought, or None if nothing describes it.

    None is a normal answer, not a failure: a product with no variations, or an
    order for a combination nobody has set up yet. The line still flows through
    on the product's own mapping.
    """
    live = [variation for variation in variations if variation.active]
    if not live:
        return None

    if line.etsy_product_id is not None:
        for variation in live:
            if variation.etsy_product_id == line.etsy_product_id:
                return variation

    chosen = option_key(line.variations)
    if not chosen:
        return None
    for variation in live:
        wanted = option_key(variation.options)
        # Subset, not equality: the buyer may have picked options this variation
        # does not care about — a colour, when the variation is only about the
        # fan. Anything the variation does pin has to match.
        if wanted and wanted <= chosen:
            return variation
    return None


@dataclass(frozen=True)
class PrintPlan:
    """Where a line's prints come from, once the variation has had its say."""

    bambuddy_archive_id: int
    plate_number: int
    units_per_plate: int
    preferred_printer_id: int | None


def print_plan(
    mapping: PrintMapping | None, variation: ProductVariation | None
) -> PrintPlan | None:
    """The product's mapping, with anything the variation overrides applied.

    A variation that names its own archive replaces the file outright; without
    one it can still adjust the plate, the yield or the printer. Returns None
    when there is nothing to print from at all.
    """
    if variation is not None and variation.bambuddy_archive_id is not None:
        return PrintPlan(
            bambuddy_archive_id=variation.bambuddy_archive_id,
            plate_number=variation.plate_number or 1,
            units_per_plate=variation.units_per_plate or 1,
            preferred_printer_id=variation.preferred_printer_id,
        )
    if mapping is None:
        return None
    return PrintPlan(
        bambuddy_archive_id=mapping.bambuddy_archive_id,
        plate_number=(
            variation.plate_number
            if variation is not None and variation.plate_number
            else mapping.plate_number
        ),
        units_per_plate=(
            variation.units_per_plate
            if variation is not None and variation.units_per_plate
            else mapping.units_per_plate
        ),
        preferred_printer_id=(
            variation.preferred_printer_id
            if variation is not None and variation.preferred_printer_id is not None
            else mapping.preferred_printer_id
        ),
    )


def stock_item(
    product: Product, variation: ProductVariation | None
) -> tuple[str | None, str | None]:
    """The QuickBooks item this line draws down, as (id, name).

    A variation tracked as its own item — one colour of filament, say — takes
    precedence over the product's.
    """
    if variation is not None and variation.qbo_item_id:
        return variation.qbo_item_id, variation.qbo_item_name
    return product.qbo_item_id, product.qbo_item_name


def from_etsy_variants(
    variants: list[dict[str, Any]], listing_id: int | None
) -> list[dict[str, Any]]:
    """Turn a listing's buyable combinations into variation rows.

    Etsy already describes exactly this on the listing, so nobody should be
    typing option names by hand and hoping they match. Combinations with no
    options at all are dropped: that is a listing without variations, and a
    single variation covering everything would only get in the way.
    """
    rows: list[dict[str, Any]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for variant in variants:
        options = [
            {"name": str(option["name"]).strip(), "value": str(option["value"]).strip()}
            for option in variant.get("options") or []
            if option.get("name") and option.get("value")
        ]
        if not options:
            continue
        key = option_key(options)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "options": options,
                "label": label_for(options),
                "etsy_listing_id": listing_id,
                "etsy_product_id": variant.get("product_id"),
            }
        )
    return rows
