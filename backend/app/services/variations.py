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

from ..models import OrderLine, PrintFile, Product, ProductVariation


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

    # One of these two identifies the file. Both can be set — an archive that is
    # also a file-manager entry — and Bambuddy takes whichever it understands.
    bambuddy_archive_id: int | None
    bambuddy_file_path: str | None
    # What to call it on a card or in an error. Not an identifier.
    name: str | None
    plate_number: int
    units_per_plate: int
    # Which printer models can take it. Empty means any of them.
    printer_models: tuple[str, ...]
    # The one machine it has to go to, when the file lives on that machine
    # rather than in the farm's shared library. None leaves the choice open.
    printer_id: int | None
    print_options: dict[str, Any]

    def as_candidate(self) -> dict[str, Any]:
        """The form a print job stores, so dispatch can choose between them."""
        return {
            "archive_id": self.bambuddy_archive_id,
            "file_path": self.bambuddy_file_path,
            "name": self.name,
            "plate_number": self.plate_number,
            "units_per_plate": self.units_per_plate,
            "printer_models": list(self.printer_models),
            "printer_id": self.printer_id,
            "print_options": self.print_options,
        }


def _names_a_file(row: PrintFile | ProductVariation) -> bool:
    return row.bambuddy_archive_id is not None or bool(row.bambuddy_file_path)


def _as_plan(row: PrintFile, variation: ProductVariation | None) -> PrintPlan:
    """One of a product's files, with anything the variation adjusts applied."""
    return PrintPlan(
        bambuddy_archive_id=row.bambuddy_archive_id,
        bambuddy_file_path=row.bambuddy_file_path,
        name=row.bambuddy_archive_name,
        plate_number=(
            variation.plate_number
            if variation is not None and variation.plate_number
            else row.plate_number
        ),
        units_per_plate=(
            variation.units_per_plate
            if variation is not None and variation.units_per_plate
            else row.units_per_plate
        ),
        printer_models=tuple(
            variation.printer_models
            if variation is not None and variation.printer_models
            else (row.printer_models or ())
        ),
        printer_id=row.bambuddy_printer_id,
        print_options=dict(row.print_options or {}),
    )


def print_plans(
    files: list[PrintFile], variation: ProductVariation | None
) -> list[PrintPlan]:
    """Every way this line could be printed, best first.

    A product has one file per way it can be made — the same part sliced for
    each machine that can take it — and they are alternatives, not a sequence.
    Which one is used is a question about which printer is free, and that is not
    knowable here; all of them come back, and the choice is made at dispatch.

    A variation that names its own file replaces the lot, because it is a
    different part rather than a different slicing of the same one, and takes
    its printer with it. Without a file of its own it adjusts each of the
    product\'s in turn.

    Ordered so that a file naming the machines it is for comes before one that
    says "anything": the specific answer is the one somebody meant.
    """
    if variation is not None and _names_a_file(variation):
        return [
            PrintPlan(
                bambuddy_archive_id=variation.bambuddy_archive_id,
                bambuddy_file_path=variation.bambuddy_file_path,
                name=variation.bambuddy_archive_name,
                plate_number=variation.plate_number or 1,
                units_per_plate=variation.units_per_plate or 1,
                printer_models=tuple(variation.printer_models or ()),
                printer_id=variation.bambuddy_printer_id,
                print_options={},
            )
        ]
    plans = [_as_plan(row, variation) for row in files if _names_a_file(row)]
    return sorted(
        plans, key=lambda plan: 0 if (plan.printer_models or plan.printer_id) else 1
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
