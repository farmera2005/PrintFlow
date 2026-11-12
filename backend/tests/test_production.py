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
    async def test_units_coming_off_the_shelf_are_not_made(self, db):
        """Picked, not made — so they are not on the make list. They are on
        the gather list instead; see TestGathering."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db)
        await _line(
            db, order, stocked, to_print=0, state="ready", quantity=3,
            qty_from_stock=3,
        )
        await db.commit()

        found = await production.to_make(db)
        assert found["items"] == []
        assert found["totals"]["units_to_make"] == 0
        assert [row["sku"] for row in found["gather"]] == ["TRINKET"]

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


class TestGathering:
    async def test_it_lists_the_components_rather_than_the_bundle(self, db):
        """Somebody assembling walks to the shelves, and what they need there
        is "two brackets", not "one gift set"."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        part = await _product(db)
        db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=2))
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=2, state="printed", parent_line_id=parent.id)
        await db.commit()

        found = await production.to_make(db)
        assert len(found["gather"]) == 1
        gather = found["gather"][0]
        # The component, with how many to fetch — and never the bundle line.
        assert (gather["sku"], gather["units"]) == ("PART-X", 2)
        assert gather["units_waiting"] == 0
        # The bundle rides along as what it is for, not as a row of its own.
        assert gather["bundles"] == ["Gift set"]
        assert {row["sku"] for row in found["gather"]} == {"PART-X"}

    async def test_components_add_up_across_bundles_and_orders(self, db):
        """Three gift sets of two brackets each is one line reading six."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        part = await _product(db)
        knob = await _product(db, sku="KNOB-S", name="Small knob")
        for index in range(3):
            order = await _order(db, number=f"940{index}", days_ago=index)
            parent = await _line(
                db, order, bundle, to_print=0, state="exploded", quantity=1
            )
            await _line(
                db, order, part, to_print=2, state="printed", parent_line_id=parent.id,
                quantity=2,
            )
            await _line(
                db, order, knob, to_print=1, state="printed", parent_line_id=parent.id,
                quantity=1,
            )
        await db.commit()

        gather = {row["sku"]: row for row in (await production.to_make(db))["gather"]}
        assert gather["PART-X"]["units"] == 6
        assert gather["KNOB-S"]["units"] == 3
        assert gather["PART-X"]["order_count"] == 3

    async def test_a_component_off_the_shelf_is_on_the_gather_list(self, db):
        """It was never made, so it is on no other list here — and the person
        assembling still has to go and find it."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        printed = await _product(db)
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, printed, to_print=1, state="printed", parent_line_id=parent.id)
        await _line(
            db, order, stocked, to_print=0, state="allocated", quantity=4,
            parent_line_id=parent.id,
        )
        await db.commit()

        gather = {row["sku"]: row["units"] for row in (await production.to_make(db))["gather"]}
        assert gather == {"PART-X": 1, "TRINKET": 4}

    async def test_a_bundle_still_missing_a_part_is_not_ready(self, db):
        """A half-made bundle on an assembly list is a bench occupied by
        something that cannot be finished."""
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        other = await _product(db, sku="PART-Y")
        await _file(db, other)
        order = await _order(db)
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=1, state="printed", parent_line_id=parent.id)
        await _line(db, order, other, to_print=1, state="new", parent_line_id=parent.id)
        await db.commit()

        found = await production.to_make(db)
        gather = {row["sku"]: row for row in found["gather"]}
        # Counted apart: a half-made bundle is a bench occupied by something
        # that cannot be finished, so its parts are not "to gather" yet.
        assert gather["PART-X"]["units"] == 0
        assert gather["PART-X"]["units_waiting"] == 1
        # And the part it is still missing is on the make list.
        assert [row["sku"] for row in found["items"]] == ["PART-Y"]

    async def test_an_assembled_bundle_has_left_the_list(self, db):
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        order = await _order(db)
        parent = await _line(
            db, order, bundle, to_print=0, state="ready", quantity=1,
            assembled_at=NOW,
        )
        await _line(db, order, part, to_print=1, state="printed", parent_line_id=parent.id)
        await db.commit()

        assert (await production.to_make(db))["gather"] == []

    async def test_a_part_says_when_it_goes_into_a_bundle(self, db):
        """Somebody picking the shelf will not find it on the order by name."""
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        await _file(db, part)
        order = await _order(db)
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=2, parent_line_id=parent.id)
        await db.commit()

        row = (await production.to_make(db))["items"][0]
        assert row["for_bundle"] is True

    async def test_an_item_that_is_not_a_bundle_is_itself_the_row(self, db):
        """The other half of the rule: a bundle becomes its components, and
        anything else has nothing to break into, so it is the line."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        for index in range(3):
            order = await _order(db, number=f"950{index}", days_ago=index)
            await _line(
                db, order, stocked, to_print=0, state="ready", quantity=2,
                qty_from_stock=2,
            )
        await db.commit()

        gather = (await production.to_make(db))["gather"]
        assert len(gather) == 1
        assert (gather[0]["sku"], gather[0]["units"]) == ("TRINKET", 6)
        # Nothing to build it into: it is the thing.
        assert gather[0]["bundles"] == []
        assert gather[0]["order_count"] == 3

    async def test_a_line_half_from_stock_is_honestly_on_both_lists(self, db):
        """Three off the shelf and two to make is three to fetch and two to
        make, not five of either."""
        part = await _product(db)
        await _file(db, part)
        order = await _order(db)
        await _line(
            db, order, part, to_print=2, state="new", quantity=5, qty_from_stock=3
        )
        await db.commit()

        found = await production.to_make(db)
        assert [(row["sku"], row["units_needed"]) for row in found["items"]] == [
            ("PART-X", 2)
        ]
        assert [(row["sku"], row["units"]) for row in found["gather"]] == [
            ("PART-X", 3)
        ]

    async def test_an_item_already_labelled_is_packed_and_gone(self, db):
        """Its parcel exists; nobody is fetching it again."""
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="ready_to_ship")
        await _line(
            db, order, stocked, to_print=0, state="labeled", quantity=2,
            qty_from_stock=2,
        )
        await db.commit()

        assert (await production.to_make(db))["gather"] == []

    async def test_a_bundles_component_is_not_counted_twice(self, db):
        """It is gathered with its bundle, and the bundle rule has already
        had it — the item rule must not take it again."""
        bundle = await _product(db, sku="GIFT-SET", name="Gift set", fulfillment="bundle")
        stocked = await _product(db, sku="TRINKET", fulfillment="stocked")
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(
            db, order, stocked, to_print=0, state="allocated", quantity=2,
            qty_from_stock=2, parent_line_id=parent.id,
        )
        await db.commit()

        gather = (await production.to_make(db))["gather"]
        assert len(gather) == 1
        assert (gather[0]["sku"], gather[0]["units"]) == ("TRINKET", 2)
        assert gather[0]["bundles"] == ["Gift set"]

    async def test_a_cancelled_component_is_not_gathered(self, db):
        bundle = await _product(db, sku="GIFT-SET", fulfillment="bundle")
        part = await _product(db)
        dropped = await _product(db, sku="PART-Y")
        order = await _order(db, status="assembly")
        parent = await _line(db, order, bundle, to_print=0, state="exploded", quantity=1)
        await _line(db, order, part, to_print=1, state="printed", parent_line_id=parent.id)
        await _line(
            db, order, dropped, to_print=0, state="cancelled", quantity=1,
            parent_line_id=parent.id,
        )
        await db.commit()

        assert [row["sku"] for row in (await production.to_make(db))["gather"]] == [
            "PART-X"
        ]


class TestTotals:
    async def test_the_tiles_add_up_the_rows(self, db):
        part = await _product(db)
        other = await _product(db, sku="PART-Y")
        blocked = await _product(db, sku="NOFILE")
        await _file(db, part, units_per_plate=4)
        await _file(db, other)
        first = await _order(db, "9001", days_ago=2)
        second = await _order(db, "9002", days_ago=1)
        line = await _line(db, first, part, to_print=8)
        await _job(db, line, status="pending", units=4)
        await _job(db, line, status="printing", units=4)
        await _line(db, second, other, to_print=3)
        await _line(db, second, blocked, to_print=1)
        await db.commit()

        totals = (await production.to_make(db))["totals"]
        # PART-X is entirely under way, so it is not a thing to make at all.
        assert totals["items"] == 2
        assert totals["units_to_make"] == 4
        assert totals["in_production_items"] == 1
        assert totals["units_in_production"] == 8
        assert totals["units_happening"] == 4
        assert totals["blocked"] == 1
        assert totals["orders"] == 2

    async def test_an_empty_shop_is_an_empty_list_not_an_error(self, db):
        found = await production.to_make(db)
        assert found["items"] == []
        assert found["in_production"] == []
        assert found["totals"]["units_to_make"] == 0
        assert found["generated_at"] is not None


class TestOverTheWire:
    async def test_the_list_comes_back_whole(self, db, signed_in):
        part = await _product(db)
        await _file(db, part, units_per_plate=4)
        await _line(db, await _order(db, "9001"), part, to_print=6)
        await db.commit()

        response = await signed_in.get("/api/production")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totals"]["units_to_make"] == 6
        assert body["items"][0]["sku"] == "PART-X"
        assert body["items"][0]["units_to_make"] == 6
        assert body["in_production"] == []
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
        await _file(db, part, units_per_plate=4)
        await _line(db, await _order(db, "9001"), part, to_print=6)
        await db.commit()

        response = await signed_in.get("/api/production.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert rows[0]["Code"] == "PART-X"
        assert rows[0]["Quantity"] == "6"
        assert rows[0]["Status"] == "To make"
        # The sheet somebody carries to a bench: things and counts, with no
        # column of order numbers to read past and nothing about machines.
        assert "Orders" not in rows[0]
        assert "Plates" not in rows[0]

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
