"""Everything the shop still has to make, gathered across every open order.

The claim this list makes is that it is a list of *things*, not of orders: six
orders wanting one bracket each are one row of six. No order appears anywhere
in it — a thing does not become several things to make because several people
bought it. Everything here is about that regrouping being right: the same
thing from different orders adding up, two variations of one product staying
apart because they are made differently, and units already under way moving
off the list somebody works from and onto the list they watch.

The other half is what a naive version would miss and quietly under-report: a
line nothing matched, a product nothing knows how to make, work that failed,
and a bundle whose parts exist but which nobody has put together yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    BomLine,
    Order,
    OrderLine,
    PrintFile,
    PrintJob,
    Product,
    ProductVariation,
)
from app.services import production

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


async def _product(db, sku="PART-X", name="Printed part X", fulfillment="printed", **fields):
    product = Product(sku=sku, name=name, fulfillment=fulfillment, **fields)
    db.add(product)
    await db.flush()
    return product


async def _file(db, product, *, units_per_plate=1, archive=501, **fields):
    row = PrintFile(
        product_id=product.id,
        bambuddy_archive_id=archive,
        units_per_plate=units_per_plate,
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def _order(db, number="9001", *, status="new", days_ago=0, **fields):
    order = Order(
        source="etsy",
        etsy_receipt_id=int(number) if str(number).isdigit() else None,
        external_id=None if str(number).isdigit() else str(number),
        order_number=str(number),
        buyer_name="Dana Buyer",
        placed_at=NOW - timedelta(days=days_ago),
        status=status,
        **fields,
    )
    db.add(order)
    await db.flush()
    return order


async def _line(db, order, product, *, to_print=1, state="new", **fields):
    line = OrderLine(
        order_id=order.id,
        product_id=product.id if product else None,
        sku_raw=product.sku if product else fields.pop("sku_raw", "MYSTERY"),
        title=product.name if product else fields.pop("title", "Unknown"),
        quantity=fields.pop("quantity", to_print),
        qty_to_print=to_print,
        state=state,
        **fields,
    )
    db.add(line)
    await db.flush()
    return line


async def _job(db, line, *, status="pending", units=1, **fields):
    job = PrintJob(
        order_line_id=line.id,
        bambuddy_archive_id=501,
        units_expected=units,
        status=status,
        **fields,
    )
    db.add(job)
    await db.flush()
    return job


def _plate(archive: int) -> list[dict]:
    """One way of making this plate: any machine, this file."""
    return [{"archive_id": archive, "plate_number": 1, "printer_models": []}]


def _by_sku(found):
    return {row["sku"]: row for row in found["items"]}


# --------------------------------------------------------------------------
# The regrouping
# --------------------------------------------------------------------------


class TestOnePartManyOrders:
    async def test_the_same_part_from_six_orders_is_one_row(self, db):
        """The whole point: finding this out used to mean opening six orders."""
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        for index in range(6):
            order = await _order(db, number=f"900{index}", days_ago=index)
            await _line(db, order, part, to_print=1)
        await db.commit()

        found = await production.to_make(db)

        assert len(found["items"]) == 1
        row = found["items"][0]
        assert (row["sku"], row["units_needed"], row["order_count"]) == ("PART-X", 6, 6)
        # Six wanted and nothing started on any of them: six to make.
        assert row["units_to_make"] == 6

    async def test_a_part_carries_no_orders_only_their_count(self, db):
        """The regrouping is the feature: the row is the thing, and which
        orders wanted it is a question for the order screens."""
        part = await _product(db)
        await _file(db, part)
        for number, days in (("9001", 1), ("9002", 9), ("9003", 4)):
            order = await _order(db, number=number, days_ago=days)
            await _line(db, order, part, to_print=2)
        await db.commit()

        row = (await production.to_make(db))["items"][0]
        assert "orders" not in row
        assert (row["units_needed"], row["order_count"]) == (6, 3)
        # Dated by the order that has waited longest, which is what sorts the
        # list even though the order itself is not on it.
        assert row["waiting_since"] == NOW - timedelta(days=9)

    async def test_the_list_itself_leads_with_what_has_waited_longest(self, db):
        """The order a shop works in; a tie goes to the bigger job, which is
        the one worth setting up for."""
        old = await _product(db, sku="OLD")
        fresh = await _product(db, sku="FRESH")
        await _file(db, old)
        await _file(db, fresh)
        await _line(db, await _order(db, "9001", days_ago=1), fresh, to_print=50)
        await _line(db, await _order(db, "9002", days_ago=20), old, to_print=1)
        await db.commit()

        assert [row["sku"] for row in (await production.to_make(db))["items"]] == [
            "OLD",
            "FRESH",
        ]

    async def test_two_variations_of_one_product_stay_apart(self, db):
        """"With fan" and "without fan" are made differently, and adding them
        together would produce a number that matches neither."""
        part = await _product(db)
        await _file(db, part)
        with_fan = ProductVariation(
            product_id=part.id, label="With fan", options=[{"name": "Fan", "value": "Yes"}]
        )
        without = ProductVariation(
            product_id=part.id, label="No fan", options=[{"name": "Fan", "value": "No"}]
        )
        db.add_all([with_fan, without])
        await db.flush()
        order = await _order(db)
        await _line(db, order, part, to_print=2, variation_id=with_fan.id)
        await _line(db, order, part, to_print=3, variation_id=without.id)
        await db.commit()

        found = await production.to_make(db)
        assert len(found["items"]) == 2
        assert {row["variation_label"] for row in found["items"]} == {"With fan", "No fan"}
        assert sorted(row["units_needed"] for row in found["items"]) == [2, 3]


# --------------------------------------------------------------------------
# What is still to make, and what is already being made
# --------------------------------------------------------------------------


class TestWhatIsStillToMake:
    async def test_units_already_under_way_are_not_still_to_make(self, db):
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        line = await _line(db, await _order(db), part, to_print=6)
        await _job(db, line, status="queued", units=4)
        await db.commit()

        found = await production.to_make(db)
        row = found["items"][0]
        assert (row["units_needed"], row["units_started"]) == (6, 4)
        assert row["units_to_make"] == 2
        # And the four that started are on the other list, not lost.
        assert found["in_production"][0]["units"] == 4

    async def test_work_that_failed_is_back_on_the_list(self, db):
        """Which is exactly why those units have to come back rather than sit
        somewhere looking finished."""
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        line = await _line(db, await _order(db), part, to_print=4)
        await _job(db, line, status="failed", units=4)
        await db.commit()

        found = await production.to_make(db)
        assert found["items"][0]["units_to_make"] == 4
        assert found["in_production"] == []

    async def test_something_finished_but_not_yet_fitted_is_being_made(self, db):
        """Made, and waiting for the rest of its line — so it is neither work
        to pick up nor gone."""
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        line = await _line(db, await _order(db), part, to_print=4, state="printing")
        await _job(db, line, status="done", units=4)
        await db.commit()

        found = await production.to_make(db)
        assert found["items"] == []
        made = found["in_production"][0]
        assert (made["units"], made["units_made"], made["progress"]) == (4, 4, 1.0)

    async def test_the_number_to_make_never_goes_negative(self, db):
        """Work started for four units against a line needing one would
        otherwise subtract from the rest of the list."""
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        line = await _line(db, await _order(db), part, to_print=1)
        await _job(db, line, status="queued", units=4)
        await db.commit()

        found = await production.to_make(db)
        assert found["items"] == []
        assert found["in_production"][0]["units_waiting"] == 4


class TestWhatIsBeingMade:
    async def test_nothing_started_is_not_being_made(self, db):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=2)
        await db.commit()

        found = await production.to_make(db)
        assert found["in_production"] == []
        assert found["items"][0]["can_start"] is True

    async def test_a_thing_being_made_says_how_far_along_it_is(self, db):
        """The three numbers always add up to what is under way: finished,
        moving, and waiting its turn."""
        part = await _product(db)
        await _file(db, part)
        line = await _line(db, await _order(db), part, to_print=9, state="printing")
        await _job(db, line, status="done", units=4)
        await _job(db, line, status="printing", units=3)
        await _job(db, line, status="pending", units=2)
        await db.commit()

        found = await production.to_make(db)
        made = found["in_production"][0]
        assert (made["units"], made["units_made"]) == (9, 4)
        assert (made["units_happening"], made["units_waiting"]) == (3, 2)
        assert made["units_made"] + made["units_happening"] + made["units_waiting"] == 9
        assert made["progress"] == pytest.approx(4 / 9, abs=0.001)
        assert made["started_at"] is not None

    async def test_the_closest_to_finished_comes_first(self, db):
        """It is the next thing to come off a machine."""
        nearly = await _product(db, sku="NEARLY")
        barely = await _product(db, sku="BARELY")
        await _file(db, nearly)
        await _file(db, barely)
        order = await _order(db)
        one = await _line(db, order, nearly, to_print=4, state="printing")
        await _job(db, one, status="done", units=3)
        await _job(db, one, status="printing", units=1)
        two = await _line(db, order, barely, to_print=4, state="printing")
        await _job(db, two, status="printing", units=4)
        await db.commit()

        found = await production.to_make(db)
        assert [row["sku"] for row in found["in_production"]] == ["NEARLY", "BARELY"]

    async def test_a_thing_can_be_on_both_lists_at_once(self, db):
        """Four of ten started is four being made and six still to make —
        the same thing, honestly counted twice."""
        part = await _product(db)
        await _file(db, part)
        line = await _line(db, await _order(db), part, to_print=10)
        await _job(db, line, status="printing", units=4)
        await db.commit()

        found = await production.to_make(db)
        assert found["items"][0]["units_to_make"] == 6
        assert found["in_production"][0]["units"] == 4

    async def test_a_product_nothing_knows_how_to_make_cannot_start(self, db):
        """It is on the list rather than missing from it: the work is real,
        and what is wrong with it is the thing to show."""
        part = await _product(db, sku="NOFILE")
        await _line(db, await _order(db), part, to_print=3)
        await db.commit()

        found = await production.to_make(db)
        row = found["items"][0]
        assert row["can_start"] is False
        assert row["units_to_make"] == 3
        assert [entry["sku"] for entry in found["blocked"]] == ["NOFILE"]

    async def test_a_variation_that_knows_its_own_way_can_start(self, db):
        """The product has no way to be made; the variation has one."""
        part = await _product(db, sku="VARONLY")
        variation = ProductVariation(
            product_id=part.id,
            label="Blue",
            options=[{"name": "Colour", "value": "Blue"}],
            bambuddy_archive_id=777,
            units_per_plate=2,
        )
        db.add(variation)
        await db.flush()
        await _line(db, await _order(db), part, to_print=4, variation_id=variation.id)
        await db.commit()

        found = await production.to_make(db)
        assert found["items"][0]["can_start"] is True
        assert found["blocked"] == []


class TestWhatIsLeftOut:
    async def test_a_line_already_made_is_not_on_the_list(self, db):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=2, state="printed")
        await db.commit()
        assert (await production.to_make(db))["items"] == []

    async def test_shipped_complete_and_cancelled_orders_ask_for_nothing(self, db):
        part = await _product(db)
        await _file(db, part)
        for index, status in enumerate(("shipped", "complete", "cancelled")):
            order = await _order(db, number=f"910{index}", status=status)
            await _line(db, order, part, to_print=5)
        await db.commit()

        assert (await production.to_make(db))["items"] == []

    async def test_an_order_being_assembled_still_wants_its_parts(self, db):
        """Only shipped, complete and cancelled stop asking — an order in any
        live column is still work."""
        part = await _product(db)
        await _file(db, part)
        for index, status in enumerate(("new", "in_production", "assembly", "ready_to_ship")):
            order = await _order(db, number=f"920{index}", status=status)
            await _line(db, order, part, to_print=1)
        await db.commit()

        assert (await production.to_make(db))["items"][0]["units_needed"] == 4


class TestLinesNobodyCanMake:
    async def test_an_unmatched_line_is_reported_rather_than_dropped(self, db):
        """It contributes to no total, so a screen that only counted what it
        understood would under-report the work."""
        order = await _order(db, "9001")
        await _line(
            db, order, None, to_print=0, state="unmatched", sku_raw="MYSTERY-9",
            title="Something nobody linked", quantity=2,
        )
        await db.commit()

        found = await production.to_make(db)
        assert found["items"] == []
        assert len(found["unmatched"]) == 1
        assert found["unmatched"][0]["sku"] == "MYSTERY-9"
        assert found["unmatched"][0]["units"] == 2

    async def test_the_same_unlinked_listing_is_one_thing_to_fix(self, db):
        """Sold six times, it is still one listing to go and link."""
        for index in range(3):
            order = await _order(db, number=f"920{index}", days_ago=index)
            await _line(
                db, order, None, to_print=0, state="unmatched", sku_raw="MYSTERY-9",
                title="Something nobody linked", quantity=2,
            )
        await db.commit()

        found = await production.to_make(db)
        assert len(found["unmatched"]) == 1
        assert (found["unmatched"][0]["units"], found["unmatched"][0]["order_count"]) == (6, 3)

    async def test_a_matched_line_is_not_in_the_unmatched_list(self, db):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=1)
        await db.commit()
        assert (await production.to_make(db))["unmatched"] == []


class TestThingsThatComeOffAShelf:
    """There is no second list for them.

    A unit off a shelf is a unit the shop does not have yet, and somebody has
    to end the day holding it either way. So it is a row like any other, with
    how much of the row is a walk written on the row.
    """

    async def test_a_stocked_item_is_a_row_like_any_other(self, db):
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        for index in range(3):
            order = await _order(db, number=f"950{index}", days_ago=index)
            await _line(
                db, order, stocked, to_print=0, state="allocated", quantity=2,
                qty_from_stock=2,
            )
        await db.commit()

        found = await production.to_make(db)
        assert len(found["items"]) == 1
        row = found["items"][0]
        assert (row["sku"], row["units"], row["units_from_stock"]) == ("TRINKET", 6, 6)
        assert row["order_count"] == 3
        # Nothing to be unable to make: it is a walk, never blocked.
        assert row["can_start"] is True
        assert found["blocked"] == []

    async def test_a_line_half_from_stock_is_one_row_of_five(self, db):
        """Three off the shelf and two off a machine is five of the thing —
        one row, because the shop is five short either way."""
        part = await _product(db)
        await _file(db, part)
        order = await _order(db)
        await _line(
            db, order, part, to_print=2, state="new", quantity=5, qty_from_stock=3
        )
        await db.commit()

        found = await production.to_make(db)
        assert len(found["items"]) == 1
        row = found["items"][0]
        assert (row["units"], row["units_to_make"], row["units_from_stock"]) == (5, 2, 3)

    async def test_a_stocked_item_already_labelled_is_packed_and_gone(self, db):
        """Its parcel exists; nobody is fetching it again."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="ready_to_ship")
        await _line(
            db, order, stocked, to_print=0, state="labeled", quantity=2,
            qty_from_stock=2,
        )
        await db.commit()

        assert (await production.to_make(db))["items"] == []

    async def test_a_stocked_item_is_still_to_fetch_until_it_is_packed(self, db):
        """A shelf does not empty itself when QuickBooks says the stock is
        there — somebody picks it when they pack it."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="ready_to_ship")
        await _line(
            db, order, stocked, to_print=0, state="ready", quantity=2,
            qty_from_stock=2,
        )
        await db.commit()

        assert (await production.to_make(db))["items"][0]["units"] == 2


class TestBundles:
    async def test_a_bundle_is_never_a_row_its_components_are(self, db):
        """Somebody at a bench makes "two brackets", not "one gift set"."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        part = await _product(db)
        await _file(db, part)
        db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=2))
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=2, state="new", parent_line_id=parent.id)
        await db.commit()

        found = await production.to_make(db)
        assert [row["sku"] for row in found["items"]] == ["PART-X"]
        row = found["items"][0]
        assert row["units"] == 2
        # The bundle rides along as what the parts are for.
        assert (row["for_bundle"], row["bundles"]) == (True, ["Gift set"])

    async def test_components_add_up_across_bundles_and_orders(self, db):
        """Three gift sets of two brackets each is one line reading six."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        part = await _product(db)
        knob = await _product(db, sku="KNOB-S", name="Small knob")
        await _file(db, part)
        await _file(db, knob)
        for index in range(3):
            order = await _order(db, number=f"940{index}", days_ago=index)
            parent = await _line(
                db, order, bundle, to_print=0, state="exploded", quantity=1
            )
            await _line(
                db, order, part, to_print=2, state="new", parent_line_id=parent.id,
                quantity=2,
            )
            await _line(
                db, order, knob, to_print=1, state="new", parent_line_id=parent.id,
                quantity=1,
            )
        await db.commit()

        items = _by_sku(await production.to_make(db))
        assert items["PART-X"]["units"] == 6
        assert items["KNOB-S"]["units"] == 3
        assert items["PART-X"]["order_count"] == 3

    async def test_a_component_off_the_shelf_is_on_the_same_list(self, db):
        """It was never made, so it used to be on no list at all here — and
        the person assembling still has to go and find it."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        printed = await _product(db)
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        await _file(db, printed)
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, printed, to_print=1, state="new", parent_line_id=parent.id)
        await _line(
            db, order, stocked, to_print=0, state="allocated", quantity=4,
            qty_from_stock=4, parent_line_id=parent.id,
        )
        await db.commit()

        items = _by_sku(await production.to_make(db))
        assert {sku: row["units"] for sku, row in items.items()} == {
            "PART-X": 1,
            "TRINKET": 4,
        }
        assert items["TRINKET"]["units_from_stock"] == 4
        assert items["PART-X"]["units_from_stock"] == 0

    async def test_only_the_part_a_bundle_is_missing_is_on_the_list(self, db):
        """The bracket is printed and in a bin; nobody has to make it again.
        What is holding the bundle up is the part that is not."""
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        other = await _product(db, sku="PART-Y")
        await _file(db, part)
        await _file(db, other)
        order = await _order(db)
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=1, state="printed", parent_line_id=parent.id)
        await _line(db, order, other, to_print=1, state="new", parent_line_id=parent.id)
        await db.commit()

        assert [row["sku"] for row in (await production.to_make(db))["items"]] == [
            "PART-Y"
        ]

    async def test_a_bundle_whose_parts_all_exist_asks_for_nothing(self, db):
        """Everything is in hand; what is left is putting it together, which
        is the board's business, not this list's."""
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=1, state="printed", parent_line_id=parent.id)
        await _line(
            db, order, stocked, to_print=0, state="labeled", quantity=2,
            qty_from_stock=2, parent_line_id=parent.id,
        )
        await db.commit()

        assert (await production.to_make(db))["items"] == []

    async def test_a_cancelled_component_is_not_wanted(self, db):
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        dropped = await _product(db, sku="PART-Y")
        await _file(db, part)
        await _file(db, dropped)
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=1, state="new", parent_line_id=parent.id)
        await _line(
            db, order, dropped, to_print=1, state="cancelled", quantity=1,
            parent_line_id=parent.id,
        )
        await db.commit()

        assert [row["sku"] for row in (await production.to_make(db))["items"]] == [
            "PART-X"
        ]

    async def test_a_bundle_line_is_not_counted_on_top_of_its_parts(self, db):
        """The gift set's own line has a quantity too. Counting it would make
        the shop owe one more thing than anybody ordered."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        part = await _product(db)
        await _file(db, part)
        order = await _order(db)
        parent = await _line(
            db, order, bundle, to_print=1, state="exploded", quantity=1,
        )
        await _line(db, order, part, to_print=2, state="new", parent_line_id=parent.id)
        await db.commit()

        found = await production.to_make(db)
        assert [(row["sku"], row["units"]) for row in found["items"]] == [("PART-X", 2)]


class TestTotals:
    async def test_the_tiles_add_up_the_rows(self, db):
        part = await _product(db)
        other = await _product(db, sku="PART-Y")
        blocked = await _product(db, sku="NOFILE")
        await _file(db, part, units_per_plate=4)
        await _file(db, other)
        first = await _order(db, "9001", days_ago=2)
        second = await _order(db, "9002", days_ago=1)
        shelf = await _product(db, sku="TRINKET", fulfillment="stocked")
        line = await _line(db, first, part, to_print=8)
        await _job(db, line, status="pending", units=4)
        await _job(db, line, status="printing", units=4)
        await _line(db, second, other, to_print=3)
        await _line(db, second, blocked, to_print=1)
        await _line(
            db, second, shelf, to_print=0, state="allocated", quantity=5,
            qty_from_stock=5,
        )
        await db.commit()

        totals = (await production.to_make(db))["totals"]
        # PART-X is entirely under way, so it is not a thing to make at all.
        assert totals["items"] == 3
        # Four off a machine and five off a shelf is nine things to get.
        assert totals["units"] == 9
        assert totals["units_to_make"] == 4
        assert totals["units_from_stock"] == 5
        assert totals["in_production_items"] == 1
        assert totals["units_in_production"] == 8
        assert totals["units_happening"] == 4
        assert totals["blocked"] == 1
        assert totals["orders"] == 2

    async def test_an_empty_shop_is_an_empty_list_not_an_error(self, db):
        found = await production.to_make(db)
        assert found["items"] == []
        assert found["in_production"] == []
        assert found["totals"]["units"] == 0
        assert found["generated_at"] is not None

    async def test_there_is_no_second_list(self, db):
        """The payload has one list of things, and no gather bucket beside
        it — a stocked item is a row on the same list as everything else."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        await _line(
            db, await _order(db), stocked, to_print=0, state="allocated",
            quantity=2, qty_from_stock=2,
        )
        await db.commit()

        found = await production.to_make(db)
        assert "gather" not in found
        assert [row["sku"] for row in found["items"]] == ["TRINKET"]


class TestOverTheWire:
    async def test_the_list_comes_back_whole(self, db, signed_in):
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        await _line(db, await _order(db, "9001"), part, to_print=6)
        await db.commit()

        response = await signed_in.get("/api/production")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totals"]["units"] == 6
        assert body["items"][0]["sku"] == "PART-X"
        assert body["items"][0]["units"] == 6
        assert body["items"][0]["units_from_stock"] == 0
        assert body["in_production"] == []
        assert "gather" not in body
        # No order travels with a thing, on the wire either.
        assert "orders" not in body["items"][0]

    async def test_the_wire_carries_what_is_being_made(self, db, signed_in):
        part = await _product(db)
        await _file(db, part)
        line = await _line(db, await _order(db, "9001"), part, to_print=4, state="printing")
        await _job(db, line, status="done", units=2)
        await _job(db, line, status="printing", units=2)
        await db.commit()

        body = (await signed_in.get("/api/production")).json()
        assert body["items"] == []
        made = body["in_production"][0]
        assert (made["units"], made["units_made"], made["units_happening"]) == (4, 2, 2)
        assert made["progress"] == 0.5

    async def test_the_csv_is_the_same_list(self, db, signed_in):
        import csv
        import io

        part = await _product(db)
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        await _file(db, part, units_per_plate=4)
        order = await _order(db, "9001")
        await _line(db, order, part, to_print=6)
        await _line(
            db, order, stocked, to_print=0, state="allocated", quantity=2,
            qty_from_stock=2,
        )
        await db.commit()

        response = await signed_in.get("/api/production.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        rows = {row["Code"]: row for row in csv.DictReader(io.StringIO(response.text))}
        assert rows["PART-X"]["Quantity"] == "6"
        assert rows["PART-X"]["From stock"] == "0"
        assert rows["PART-X"]["Status"] == "To make"
        # One sheet, with the shelf said on the row rather than further down.
        assert rows["TRINKET"]["Quantity"] == "2"
        assert rows["TRINKET"]["From stock"] == "2"
        assert rows["TRINKET"]["Status"] == "To make"
        # The sheet somebody carries to a bench: things and counts, with no
        # column of order numbers to read past and nothing about machines.
        assert "Orders" not in rows["PART-X"]
        assert "Plates" not in rows["PART-X"]

    async def test_the_csv_says_what_is_being_made(self, db, signed_in):
        import csv
        import io

        part = await _product(db)
        await _file(db, part)
        line = await _line(db, await _order(db, "9001"), part, to_print=3, state="printing")
        await _job(db, line, status="done", units=1)
        await _job(db, line, status="printing", units=2)
        await db.commit()

        response = await signed_in.get("/api/production.csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 1
        assert rows[0]["Status"] == "Being made (1 of 3 done)"

    async def test_it_takes_a_signed_in_user(self, db, client):
        assert (await client.get("/api/production")).status_code == 401
        assert (await client.get("/api/production.csv")).status_code == 401


class TestWhatGoesFirst:
    """The shop's own ordering, on top of the one the dates give it.

    Longest wait first is right until the afternoon somebody is on the phone.
    What matters about this list is that saying so is cheap, visible and
    reversible — and that it is not decoration: a thing pinned here is a
    thing a machine actually takes first.
    """

    async def test_nothing_is_first_until_somebody_says_so(self, db):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=2)
        await db.commit()

        found = await production.to_make(db)
        assert found["priority"] == []
        assert [row["sku"] for row in found["items"]] == ["PART-X"]

    async def test_a_pinned_thing_leaves_the_queue_for_the_front(self, db):
        old = await _product(db, sku="OLD")
        rush = await _product(db, sku="RUSH")
        await _file(db, old)
        await _file(db, rush)
        await _line(db, await _order(db, "9001", days_ago=20), old, to_print=1)
        await _line(db, await _order(db, "9002", days_ago=1), rush, to_print=1)
        await db.commit()

        await production.set_priority(db, [(rush.id, None)], by="adam")
        await db.commit()

        found = await production.to_make(db)
        # In front of the thing that has waited three weeks, which is the
        # whole point: the dates are a default, not a verdict.
        assert [row["sku"] for row in found["priority"]] == ["RUSH"]
        assert [row["sku"] for row in found["items"]] == ["OLD"]

    async def test_the_order_it_was_dragged_into_is_the_order_it_keeps(self, db):
        first = await _product(db, sku="ONE")
        second = await _product(db, sku="TWO")
        third = await _product(db, sku="THREE")
        order = await _order(db)
        for product in (first, second, third):
            await _file(db, product)
            await _line(db, order, product, to_print=1)
        await db.commit()

        await production.set_priority(
            db, [(third.id, None), (first.id, None), (second.id, None)]
        )
        await db.commit()
        assert [row["sku"] for row in (await production.to_make(db))["priority"]] == [
            "THREE",
            "ONE",
            "TWO",
        ]

        # Dragged again, and the new order is the order — not merged with
        # the old one, which is how two people dragging at once ends up with
        # a list neither of them chose.
        await production.set_priority(
            db, [(second.id, None), (third.id, None), (first.id, None)]
        )
        await db.commit()
        assert [row["sku"] for row in (await production.to_make(db))["priority"]] == [
            "TWO",
            "THREE",
            "ONE",
        ]

    async def test_dragging_it_back_puts_it_where_its_wait_belongs(self, db):
        old = await _product(db, sku="OLD")
        rush = await _product(db, sku="RUSH")
        await _file(db, old)
        await _file(db, rush)
        await _line(db, await _order(db, "9001", days_ago=20), old, to_print=1)
        await _line(db, await _order(db, "9002", days_ago=1), rush, to_print=1)
        await db.commit()

        await production.set_priority(db, [(rush.id, None)])
        await db.commit()
        await production.set_priority(db, [])
        await db.commit()

        found = await production.to_make(db)
        assert found["priority"] == []
        assert [row["sku"] for row in found["items"]] == ["OLD", "RUSH"]

    async def test_two_variations_are_pinned_separately(self, db):
        """They are two different things to make, so "first" is a thing that
        can be true of one and not the other."""
        part = await _product(db)
        await _file(db, part)
        blue = ProductVariation(
            product_id=part.id, label="Blue", options=[{"name": "Colour", "value": "Blue"}]
        )
        red = ProductVariation(
            product_id=part.id, label="Red", options=[{"name": "Colour", "value": "Red"}]
        )
        db.add_all([blue, red])
        await db.flush()
        order = await _order(db)
        await _line(db, order, part, to_print=1, variation_id=blue.id)
        await _line(db, order, part, to_print=1, variation_id=red.id)
        await db.commit()

        await production.set_priority(db, [(part.id, red.id)])
        await db.commit()

        found = await production.to_make(db)
        assert [row["variation_label"] for row in found["priority"]] == ["Red"]
        assert [row["variation_label"] for row in found["items"]] == ["Blue"]

    async def test_a_pinned_thing_with_nothing_to_do_still_shows(self, db):
        """Otherwise it is invisible, and an invisible pin is one nobody can
        take off — which is how a list like this silently rots."""
        part = await _product(db)
        await _file(db, part)
        await db.commit()

        await production.set_priority(db, [(part.id, None)])
        await db.commit()

        found = await production.to_make(db)
        assert len(found["priority"]) == 1
        row = found["priority"][0]
        assert (row["sku"], row["units"]) == ("PART-X", 0)
        # And it is not work, so it is not in the count.
        assert found["totals"]["items"] == 0
        assert found["totals"]["units"] == 0

    async def test_a_pin_survives_the_thing_being_made_and_wanted_again(self, db):
        """It stays until somebody takes it off. A pin that cleared itself
        the moment a backlog emptied would come back next week with nobody
        able to say why."""
        part = await _product(db)
        await _file(db, part)
        await production.set_priority(db, [(part.id, None)])
        await db.commit()

        await _line(db, await _order(db, "9100"), part, to_print=4)
        await db.commit()

        found = await production.to_make(db)
        assert [(row["sku"], row["units"]) for row in found["priority"]] == [
            ("PART-X", 4)
        ]
        assert found["items"] == []

    async def test_the_same_thing_twice_is_pinned_once(self, db):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=1)
        await db.commit()

        kept = await production.set_priority(
            db, [(part.id, None), (part.id, None)]
        )
        await db.commit()
        assert kept == [(part.id, None)]
        assert len((await production.to_make(db))["priority"]) == 1

    async def test_a_thing_that_does_not_exist_cannot_be_pinned(self, db):
        import uuid as _uuid

        with pytest.raises(ValueError):
            await production.set_priority(db, [(_uuid.uuid4(), None)])

    async def test_a_variation_of_another_product_cannot_be_pinned(self, db):
        """Nothing could ever match it, so the row would sit there for ever
        meaning nothing."""
        part = await _product(db)
        other = await _product(db, sku="PART-Y")
        variation = ProductVariation(
            product_id=other.id,
            label="Blue",
            options=[{"name": "Colour", "value": "Blue"}],
        )
        db.add(variation)
        await db.flush()
        await db.commit()

        with pytest.raises(ValueError):
            await production.set_priority(db, [(part.id, variation.id)])

    async def test_pinning_follows_the_product_out_of_the_catalogue(self, db):
        """Delete the product and the pin goes with it, rather than becoming
        a row pointing at nothing."""
        from sqlalchemy import delete, select as _select

        from app.models import ProductionPriority

        part = await _product(db)
        await db.commit()
        await production.set_priority(db, [(part.id, None)])
        await db.commit()

        await db.execute(delete(Product).where(Product.id == part.id))
        await db.commit()

        left = (await db.execute(_select(ProductionPriority))).scalars().all()
        assert left == []


class TestFirstOverTheWire:
    async def test_dragging_a_row_up_comes_back_as_the_whole_screen(self, db, signed_in):
        old = await _product(db, sku="OLD")
        rush = await _product(db, sku="RUSH")
        await _file(db, old)
        await _file(db, rush)
        await _line(db, await _order(db, "9001", days_ago=20), old, to_print=1)
        await _line(db, await _order(db, "9002", days_ago=1), rush, to_print=1)
        await db.commit()

        response = await signed_in.put(
            "/api/production/priority",
            json={"items": [{"product_id": str(rush.id)}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # The answer is the list as it now reads, so the screen never has to
        # guess what the server did with it.
        assert [row["sku"] for row in body["priority"]] == ["RUSH"]
        assert [row["sku"] for row in body["items"]] == ["OLD"]

        again = (await signed_in.get("/api/production")).json()
        assert [row["sku"] for row in again["priority"]] == ["RUSH"]

    async def test_an_empty_list_clears_everything(self, db, signed_in):
        part = await _product(db)
        await _file(db, part)
        await _line(db, await _order(db), part, to_print=1)
        await db.commit()
        await signed_in.put(
            "/api/production/priority",
            json={"items": [{"product_id": str(part.id)}]},
        )

        body = (
            await signed_in.put("/api/production/priority", json={"items": []})
        ).json()
        assert body["priority"] == []
        assert [row["sku"] for row in body["items"]] == ["PART-X"]

    async def test_a_thing_that_does_not_exist_is_a_bad_request(self, db, signed_in):
        import uuid as _uuid

        response = await signed_in.put(
            "/api/production/priority",
            json={"items": [{"product_id": str(_uuid.uuid4())}]},
        )
        assert response.status_code == 400

    async def test_the_list_is_capped(self, db, signed_in):
        """It arrives from a browser, and a priority list is a handful of
        things somebody reordered by hand."""
        import uuid as _uuid

        response = await signed_in.put(
            "/api/production/priority",
            json={"items": [{"product_id": str(_uuid.uuid4())} for _ in range(201)]},
        )
        assert response.status_code == 422

    async def test_the_csv_carries_the_order_in_a_column(self, db, signed_in):
        import csv
        import io

        old = await _product(db, sku="OLD")
        rush = await _product(db, sku="RUSH")
        await _file(db, old)
        await _file(db, rush)
        await _line(db, await _order(db, "9001", days_ago=20), old, to_print=1)
        await _line(db, await _order(db, "9002", days_ago=1), rush, to_print=1)
        await db.commit()
        await signed_in.put(
            "/api/production/priority",
            json={"items": [{"product_id": str(rush.id)}]},
        )

        rows = list(
            csv.DictReader(
                io.StringIO((await signed_in.get("/api/production.csv")).text)
            )
        )
        assert [row["Code"] for row in rows] == ["RUSH", "OLD"]
        # A number rather than the row order alone, so it survives the first
        # thing anybody does to a CSV, which is sort it.
        assert (rows[0]["First"], rows[1]["First"]) == ("1", "")

    async def test_it_takes_a_signed_in_user(self, db, client):
        assert (
            await client.put("/api/production/priority", json={"items": []})
        ).status_code == 401


class TestFirstReachesTheMachines:
    """A priority list that only reordered a screen would be worse than none.

    The whole claim the tab makes is "this goes first". If the next plate off
    the queue is still whatever was planned earliest, the shop has a control
    that looks like it works, which is how people stop trusting a tool.
    """

    @pytest.fixture
    def farm(self, monkeypatch):
        from test_intake_pipeline import FakeBambuddy

        fake = FakeBambuddy()

        async def client_for(_session):
            return fake

        monkeypatch.setattr(
            "app.services.printing.bambuddy_api.client_for", client_for
        )
        return fake

    async def test_a_pinned_thing_jumps_the_queue_to_a_machine(self, db, farm):
        from app.services import printing

        old = await _product(db, sku="OLD")
        rush = await _product(db, sku="RUSH")
        await _file(db, old, archive=601)
        await _file(db, rush, archive=602)
        order = await _order(db)
        # Planned in this order, so without a pin this is the order they go.
        older = await _line(db, order, old, to_print=1)
        await _job(db, older, status="pending", units=1, candidates=_plate(601))
        await db.commit()
        newer = await _line(db, order, rush, to_print=1)
        await _job(db, newer, status="pending", units=1, candidates=_plate(602))
        await db.commit()

        await production.set_priority(db, [(rush.id, None)])
        await db.commit()

        assert await printing.dispatch_pending(db) == {"dispatched": 2, "failed": 0}
        assert [sent["archive_id"] for sent in farm.enqueued] == [602, 601]

    async def test_without_a_pin_the_oldest_plate_still_goes_first(self, db, farm):
        """The default has to survive the feature: a shop that pins nothing
        should see exactly what it saw before."""
        from app.services import printing

        old = await _product(db, sku="OLD")
        other = await _product(db, sku="OTHER")
        await _file(db, old, archive=601)
        await _file(db, other, archive=602)
        order = await _order(db)
        older = await _line(db, order, old, to_print=1)
        await _job(db, older, status="pending", units=1, candidates=_plate(601))
        await db.commit()
        newer = await _line(db, order, other, to_print=1)
        await _job(db, newer, status="pending", units=1, candidates=_plate(602))
        await db.commit()

        await printing.dispatch_pending(db)
        assert [sent["archive_id"] for sent in farm.enqueued] == [601, 602]
