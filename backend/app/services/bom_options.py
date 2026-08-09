"""Etsy options that change what a bundle is made of.

A buyer choosing "Color: Red" does not change what the shop sells — it changes
what comes off the shelf. Red filament instead of grey, the same finished good.
Modelling each colour as its own product would split that finished good in
QuickBooks for no reason and multiply every BOM and print mapping by the number
of colours, so the option edits the BOM on the way through instead.

Pure functions: no session, no I/O. What a set of chosen options does to a bill
of materials is arithmetic, and it is the part that has to be right, so it is
the part that is testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import BomLine, BomOptionRule, Product


def normalize(raw: str | None) -> str:
    """Trimmed and case-folded, matching how SKUs are compared."""
    return (raw or "").strip().casefold()


def chosen_pairs(variations: Iterable[dict[str, Any]] | None) -> set[tuple[str, str]]:
    """The (name, value) pairs a buyer picked, normalised for matching.

    Free-text personalisation is skipped: it is for a human to read, and
    matching a rule against an arbitrary engraving message would fire on
    coincidence.
    """
    pairs: set[tuple[str, str]] = set()
    for variation in variations or []:
        if not isinstance(variation, dict) or variation.get("free_text"):
            continue
        name = normalize(variation.get("name"))
        value = normalize(variation.get("value"))
        if name and value:
            pairs.add((name, value))
    return pairs


@dataclass
class Resolved:
    """What a bundle actually needs, once the chosen options are applied."""

    components: list[tuple[Product, int]] = field(default_factory=list)
    # Rules that fired, for showing on the order line.
    applied: list[str] = field(default_factory=list)
    # Things the operator should know about but which must not stop the order.
    warnings: list[str] = field(default_factory=list)


def resolve(
    bom: list[BomLine],
    rules: list[BomOptionRule],
    variations: Iterable[dict[str, Any]] | None,
) -> Resolved:
    """Apply the chosen options to a bill of materials.

    Rules with `replaces_id` swap a component out; rules without add one. The
    result keeps the BOM's own order so a component's position on the board does
    not jump around because a rule fired.
    """
    result = Resolved()
    picked = chosen_pairs(variations)

    # product id -> [product, quantity], insertion-ordered.
    components: dict[Any, list[Any]] = {}
    for bom_line in bom:
        if bom_line.component is None:
            continue
        entry = components.get(bom_line.component_id)
        if entry is None:
            components[bom_line.component_id] = [bom_line.component, bom_line.quantity]
        else:
            entry[1] += bom_line.quantity

    for rule in rules:
        if (normalize(rule.option_name), normalize(rule.option_value)) not in picked:
            continue
        if rule.component is None:
            continue

        if rule.replaces_id is not None:
            removed = components.pop(rule.replaces_id, None)
            if removed is None:
                # The rule is stale: the component it swaps out is not in this
                # BOM any more. Adding the replacement anyway would silently
                # inflate the build, so say so and change nothing.
                name = rule.replaces.sku if rule.replaces else str(rule.replaces_id)
                result.warnings.append(
                    f"{rule.option_name} = {rule.option_value} swaps out {name}, "
                    "which is not in this BOM. The rule was skipped."
                )
                continue
            quantity = rule.quantity if rule.quantity is not None else removed[1]
            _merge(components, rule.component, quantity)
            result.applied.append(
                f"{rule.option_name} = {rule.option_value}: "
                f"{removed[0].sku} → {rule.component.sku}"
            )
        else:
            quantity = rule.quantity if rule.quantity is not None else 1
            _merge(components, rule.component, quantity)
            result.applied.append(
                f"{rule.option_name} = {rule.option_value}: added {rule.component.sku}"
            )

    result.warnings.extend(_uncovered(rules, variations, picked))
    result.components = [(product, quantity) for product, quantity in components.values()]
    return result


def _merge(components: dict[Any, list[Any]], product: Product, quantity: int) -> None:
    entry = components.get(product.id)
    if entry is None:
        components[product.id] = [product, quantity]
    else:
        entry[1] += quantity


def _uncovered(
    rules: list[BomOptionRule],
    variations: Iterable[dict[str, Any]] | None,
    picked: set[tuple[str, str]],
) -> list[str]:
    """Options this bundle has rules for, but not for the value that arrived.

    Silence here is the dangerous case: the order would be built from the base
    BOM, look entirely normal, and be the wrong colour. Only option names the
    bundle already has rules for are reported — every other option on the
    listing is none of the BOM's business.
    """
    covered_names = {normalize(rule.option_name) for rule in rules}
    if not covered_names:
        return []
    matched = {
        (normalize(rule.option_name), normalize(rule.option_value))
        for rule in rules
    }
    warnings: list[str] = []
    for variation in variations or []:
        if not isinstance(variation, dict) or variation.get("free_text"):
            continue
        name, value = normalize(variation.get("name")), normalize(variation.get("value"))
        if not name or not value or name not in covered_names:
            continue
        if (name, value) in matched:
            continue
        warnings.append(
            f"No rule for {variation.get('name')} = {variation.get('value')}. "
            "The base BOM was used — check that is what you meant to make."
        )
    return warnings
