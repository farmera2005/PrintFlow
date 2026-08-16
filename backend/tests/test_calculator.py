"""What a printed part costs to make.

Nothing here reaches an accounting system, so the stakes are lower than the
books suite — but a shop prices from these numbers, and a calculator that is
quietly wrong is worse than none at all. What these pin is the arithmetic, the
rounding, and the two behaviours a person will notice first: that the breakdown
adds up to the total, and that an empty box means nothing rather than an error.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import settings_store
from app.services.costing import Part, Rates, amount, estimate

pytestmark = pytest.mark.asyncio


RATES = Rates(
    machine_per_hour=Decimal("1.50"),
    printer_watts=Decimal("150"),
    electricity_per_kwh=Decimal("0.16"),
    labour_per_hour=Decimal("30"),
    failure_percent=Decimal("0"),
    markup_percent=Decimal("0"),
)


def _lines(result):
    return {line["key"]: Decimal(line["amount"]) for line in result["lines"]}


class TestEachCost:
    async def test_filament_is_grams_off_the_spool(self):
        part = Part(grams=Decimal("48"), filament_per_kg=Decimal("22.50"))
        # 48g of a 22.50 kilo.
        assert _lines(estimate(part, RATES))["filament"] == Decimal("1.08")

    async def test_electricity_is_watts_over_the_hours(self):
        part = Part(print_minutes=Decimal("240"))
        # 4h × 0.15 kW × 0.16 = 0.096, which is 0.10 in money.
        assert _lines(estimate(part, RATES))["electricity"] == Decimal("0.10")

    async def test_machine_time_is_the_rate_by_the_hours(self):
        part = Part(print_minutes=Decimal("252"))  # 4h 12m
        assert _lines(estimate(part, RATES))["machine"] == Decimal("6.30")

    async def test_labour_is_charged_by_the_minute(self):
        """A plate change is six minutes, not a tenth of an hour."""
        part = Part(labour_minutes=Decimal("6"))
        assert _lines(estimate(part, RATES))["labour"] == Decimal("3.00")

    async def test_extras_pass_straight_through(self):
        part = Part(extras_each=Decimal("0.40"))
        assert _lines(estimate(part, RATES))["extras"] == Decimal("0.40")


class TestTheTotal:
    async def test_the_breakdown_adds_up_to_the_subtotal(self):
        """A column of numbers that does not add up is worse than no column.

        Every line is rounded before anything is summed, precisely so that what
        is on screen agrees with itself.
        """
        part = Part(
            grams=Decimal("48"),
            filament_per_kg=Decimal("22.50"),
            print_minutes=Decimal("252"),
            labour_minutes=Decimal("6"),
            extras_each=Decimal("0.40"),
        )
        result = estimate(part, RATES)

        assert sum(_lines(result).values()) == Decimal(result["subtotal"])

    async def test_the_failure_allowance_covers_everything_above_it(self):
        """A failure wastes the machine hour as surely as the filament."""
        part = Part(grams=Decimal("100"), filament_per_kg=Decimal("20"))
        rates = Rates(machine_per_hour=Decimal("0"), failure_percent=Decimal("10"))

        result = estimate(part, rates)

        assert result["subtotal"] == "2.00"
        assert result["failure"] == "0.20"
        assert result["unit_cost"] == "2.20"

    async def test_quantity_multiplies_the_rounded_unit_cost(self):
        """Ten of them cost ten times what one costs, to the penny shown."""
        part = Part(grams=Decimal("100"), filament_per_kg=Decimal("20"), quantity=10)

        result = estimate(part, RATES)

        assert result["unit_cost"] == "2.00"
        assert result["total_cost"] == "20.00"

    async def test_markup_turns_a_cost_into_a_price(self):
        part = Part(grams=Decimal("100"), filament_per_kg=Decimal("20"))
        rates = Rates(markup_percent=Decimal("150"))

        result = estimate(part, rates)

        assert result["unit_cost"] == "2.00"
        assert result["unit_price"] == "5.00"
        assert result["profit"] == "3.00"
        # Margin, not markup: what is kept out of the price, which is the number
        # a marketplace's cut gets compared with.
        assert result["margin_percent"] == "60.00"

    async def test_no_markup_means_no_margin_rather_than_a_division_by_zero(self):
        result = estimate(Part(), Rates())
        assert result["unit_price"] == "0.00"
        assert result["margin_percent"] == "0"

    async def test_it_says_how_long_the_machine_is_tied_up(self):
        """An eight-hour part at a good margin can still be the wrong print."""
        part = Part(print_minutes=Decimal("240"), quantity=3)
        result = estimate(part, RATES)
        assert result["print_hours"] == "4.00"
        assert result["machine_hours_total"] == "12.00"


class TestEmptyBoxes:
    async def test_an_empty_calculator_is_zero_not_an_error(self):
        """One that refuses to work until every box is filled is one nobody uses."""
        result = estimate(Part(), Rates())
        assert result["unit_cost"] == "0.00"
        assert result["total_cost"] == "0.00"

    async def test_blanks_and_rubbish_read_as_nothing(self):
        assert amount(None) == Decimal("0")
        assert amount("") == Decimal("0")
        assert amount("abc") == Decimal("0")

    async def test_a_negative_is_not_a_discount(self):
        """Nothing here costs less than nothing, and −5 g is a typo."""
        assert amount("-5") == Decimal("0")

    async def test_a_line_with_nothing_behind_it_has_no_explanation(self):
        """The detail under a figure exists to be checked; "0 g at 0" is noise."""
        result = estimate(Part(), Rates())
        assert all(line["detail"] is None for line in result["lines"])


class TestFromTheWire:
    async def test_hours_and_minutes_are_added_up(self):
        """A slicer says 4h 12m, and nobody wants to divide 12 by 60."""
        part = Part.from_request({"print_hours": "4", "print_minutes": "12"})
        assert part.print_minutes == Decimal("252")

    async def test_quantity_is_at_least_one(self):
        assert Part.from_request({"quantity": 0}).quantity == 1
        assert Part.from_request({"quantity": ""}).quantity == 1
        assert Part.from_request({}).quantity == 1

    async def test_a_decimal_string_survives_the_trip(self):
        """Strings rather than floats: this is money and a float cannot hold 0.1."""
        part = Part.from_request({"filament_per_kg": "22.50"})
        assert part.filament_per_kg == Decimal("22.50")


class TestTheApi:
    async def test_rates_are_remembered_between_visits(self, signed_in):
        await signed_in.put(
            "/api/calculator", json={"machine_per_hour": "1.50", "labour_per_hour": "30"}
        )

        body = (await signed_in.get("/api/calculator")).json()

        assert body["settings"]["machine_per_hour"] == "1.50"
        assert body["settings"]["labour_per_hour"] == "30"

    async def test_an_estimate_uses_the_saved_rates(self, signed_in):
        await signed_in.put("/api/calculator", json={"machine_per_hour": "1.50"})

        body = (
            await signed_in.post("/api/calculator/estimate", json={"print_hours": "4"})
        ).json()

        assert body["unit_cost"] == "6.00"

    async def test_a_sent_rate_beats_the_saved_one_without_changing_it(self, signed_in):
        """"What would this cost at a different rate" must not rewrite the shop."""
        await signed_in.put("/api/calculator", json={"machine_per_hour": "1.50"})

        body = (
            await signed_in.post(
                "/api/calculator/estimate",
                json={"print_hours": "4", "machine_per_hour": "3"},
            )
        ).json()
        assert body["unit_cost"] == "12.00"

        saved = (await signed_in.get("/api/calculator")).json()
        assert saved["settings"]["machine_per_hour"] == "1.50"

    async def test_filaments_are_kept_and_the_nameless_dropped(self, signed_in):
        response = await signed_in.put(
            "/api/calculator",
            json={
                "filaments": [
                    {"name": "PLA Matte", "price_per_kg": "22.50"},
                    {"name": "  ", "price_per_kg": "9"},
                ]
            },
        )

        rows = response.json()["settings"]["filaments"]
        assert [row["name"] for row in rows] == ["PLA Matte"]

    async def test_nothing_is_configured_by_default(self, db):
        """A machine rate nobody chose would look worked out and would not be."""
        settings = await settings_store.get_calculator_settings(db)
        assert settings["machine_per_hour"] == ""
        assert settings["markup_percent"] == ""

    async def test_the_estimate_is_read_only(self, signed_in):
        """It is a calculator. It must not leave anything behind."""
        before = (await signed_in.get("/api/calculator")).json()
        await signed_in.post(
            "/api/calculator/estimate",
            json={"grams": "48", "filament_per_kg": "22.50", "machine_per_hour": "9"},
        )
        after = (await signed_in.get("/api/calculator")).json()
        assert before == after

    async def test_signing_in_is_required(self, client):
        assert (await client.post("/api/calculator/estimate", json={})).status_code == 401
        assert (await client.get("/api/calculator")).status_code == 401
