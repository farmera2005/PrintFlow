"""What a printed part costs to make.

Answers the question a shop asks before it prices anything: *what did that
actually cost me?* Five things go into it, and they are kept apart on purpose —
a total nobody can take apart is a total nobody argues with, and the argument
is the point. A part that looks expensive is usually expensive for one reason,
and the breakdown says which.

* **Filament** — grams off the spool at what the spool cost.
* **Electricity** — how long the machine ran, at what it draws and what a unit
  costs. Small per part and not small per month.
* **Machine time** — the printer wearing out. A rate per hour that covers the
  machine's own price over its life plus nozzles, belts and the afternoon spent
  fixing it. Usually the largest line after filament, and the one shops forget.
* **Labour** — slicing, plate changes, supports off, sanding. Minutes rather
  than hours, because that is how it is actually spent.
* **Extras** — packaging, an insert, whatever else goes out with it.

Then two adjustments that are not costs of a *successful* print but are costs of
printing:

* **Failure allowance** — a percentage, because some prints fail and the ones
  that work have to pay for the ones that did not.
* **Markup** — what turns a cost into a price. Kept here so the two are read
  together; the shop decides what the number is.

Every figure is Decimal and every line is rounded to cents before it is added
up, so the breakdown on screen sums to the total on screen. A column of numbers
that does not add up is worse than no column at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .manufacturing import money

# An hour has this many minutes, and a kilogram this many grams. Named because
# a bare 1000 in the middle of a cost calculation is a question rather than an
# answer.
MINUTES_PER_HOUR = Decimal("60")
GRAMS_PER_KILO = Decimal("1000")
WATTS_PER_KILOWATT = Decimal("1000")


def amount(value: Any) -> Decimal:
    """Anything the wire sends as a number, as a Decimal, never negative.

    Strings rather than floats wherever the caller can manage it: this is money
    and a float cannot hold 0.1. A blank field is nothing, not an error — a
    calculator that refuses to work until every box is filled is a calculator
    nobody reaches for.
    """
    if value is None or value == "":
        return Decimal("0")
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed > 0 else Decimal("0")


@dataclass(frozen=True)
class Rates:
    """What the shop charges itself, once, for everything it prints."""

    machine_per_hour: Decimal = Decimal("0")
    printer_watts: Decimal = Decimal("0")
    electricity_per_kwh: Decimal = Decimal("0")
    labour_per_hour: Decimal = Decimal("0")
    failure_percent: Decimal = Decimal("0")
    markup_percent: Decimal = Decimal("0")

    @classmethod
    def from_settings(cls, values: dict[str, Any]) -> Rates:
        return cls(
            machine_per_hour=amount(values.get("machine_per_hour")),
            printer_watts=amount(values.get("printer_watts")),
            electricity_per_kwh=amount(values.get("electricity_per_kwh")),
            labour_per_hour=amount(values.get("labour_per_hour")),
            failure_percent=amount(values.get("failure_percent")),
            markup_percent=amount(values.get("markup_percent")),
        )


@dataclass(frozen=True)
class Part:
    """One thing being printed, as the slicer describes it."""

    grams: Decimal = Decimal("0")
    filament_per_kg: Decimal = Decimal("0")
    print_minutes: Decimal = Decimal("0")
    labour_minutes: Decimal = Decimal("0")
    extras_each: Decimal = Decimal("0")
    quantity: int = 1

    @classmethod
    def from_request(cls, values: dict[str, Any]) -> Part:
        # Hours and minutes are two boxes because a slicer says "4h 12m" and
        # nobody wants to divide 12 by 60 to use this.
        minutes = (
            amount(values.get("print_hours")) * MINUTES_PER_HOUR
            + amount(values.get("print_minutes"))
        )
        raw_quantity = values.get("quantity")
        try:
            quantity = max(1, int(raw_quantity)) if raw_quantity not in (None, "") else 1
        except (TypeError, ValueError):
            quantity = 1
        return cls(
            grams=amount(values.get("grams")),
            filament_per_kg=amount(values.get("filament_per_kg")),
            print_minutes=minutes,
            labour_minutes=amount(values.get("labour_minutes")),
            extras_each=amount(values.get("extras_each")),
            quantity=quantity,
        )


def estimate(part: Part, rates: Rates) -> dict[str, Any]:
    """The cost of one of these, and of all of them, broken into its parts.

    Every line is rounded to cents before anything is added, so what is shown
    adds up to what is shown. Rounding once at the end would be a penny more
    accurate and a great deal less trustworthy.
    """
    hours = part.print_minutes / MINUTES_PER_HOUR

    filament = money(part.grams / GRAMS_PER_KILO * part.filament_per_kg)
    electricity = money(
        hours * (rates.printer_watts / WATTS_PER_KILOWATT) * rates.electricity_per_kwh
    )
    machine = money(hours * rates.machine_per_hour)
    labour = money(part.labour_minutes / MINUTES_PER_HOUR * rates.labour_per_hour)
    extras = money(part.extras_each)

    subtotal = money(filament + electricity + machine + labour + extras)
    # Not a cost of this print — a share of the ones that failed. Applied to
    # everything above it, because a failure wastes the machine hour and the
    # electricity just as surely as it wastes the filament.
    failure = money(subtotal * rates.failure_percent / Decimal("100"))
    unit_cost = money(subtotal + failure)

    total_cost = money(unit_cost * part.quantity)
    unit_price = money(unit_cost * (Decimal("1") + rates.markup_percent / Decimal("100")))
    total_price = money(unit_price * part.quantity)
    profit = money(unit_price - unit_cost)
    # Margin, not markup: what the shop keeps out of the price, which is the
    # number that gets compared with a marketplace's cut.
    margin = (
        money(profit / unit_price * Decimal("100")) if unit_price > 0 else Decimal("0")
    )

    return {
        "lines": [
            {"key": "filament", "label": "Filament", "amount": str(filament),
             "detail": _filament_detail(part)},
            {"key": "electricity", "label": "Electricity", "amount": str(electricity),
             "detail": _power_detail(hours, rates)},
            {"key": "machine", "label": "Machine time", "amount": str(machine),
             "detail": _machine_detail(hours, rates)},
            {"key": "labour", "label": "Labour", "amount": str(labour),
             "detail": _labour_detail(part, rates)},
            {"key": "extras", "label": "Extras", "amount": str(extras), "detail": None},
        ],
        "subtotal": str(subtotal),
        "failure": str(failure),
        "failure_percent": str(rates.failure_percent),
        "unit_cost": str(unit_cost),
        "quantity": part.quantity,
        "total_cost": str(total_cost),
        "markup_percent": str(rates.markup_percent),
        "unit_price": str(unit_price),
        "total_price": str(total_price),
        "profit": str(profit),
        "margin_percent": str(margin),
        # What the machine is tied up for, which is the other thing a shop is
        # spending: an eight-hour part at a good margin can still be the wrong
        # thing to print.
        "print_hours": str(money(hours)),
        "machine_hours_total": str(money(hours * part.quantity)),
    }


def _filament_detail(part: Part) -> str | None:
    if not part.grams:
        return None
    return f"{_plain(part.grams)} g at {_plain(part.filament_per_kg)} per kg"


def _power_detail(hours: Decimal, rates: Rates) -> str | None:
    if not (hours and rates.printer_watts):
        return None
    return (
        f"{_plain(money(hours))} h at {_plain(rates.printer_watts)} W, "
        f"{_plain(rates.electricity_per_kwh)} per kWh"
    )


def _machine_detail(hours: Decimal, rates: Rates) -> str | None:
    if not (hours and rates.machine_per_hour):
        return None
    return f"{_plain(money(hours))} h at {_plain(rates.machine_per_hour)} per hour"


def _labour_detail(part: Part, rates: Rates) -> str | None:
    if not (part.labour_minutes and rates.labour_per_hour):
        return None
    return (
        f"{_plain(part.labour_minutes)} min at {_plain(rates.labour_per_hour)} per hour"
    )


def _plain(value: Decimal) -> str:
    """A number without trailing zeros — "4" rather than "4.00" in prose."""
    trimmed = value.normalize()
    # normalize() turns 100 into 1E+2, which is true and unreadable.
    return format(trimmed, "f")
