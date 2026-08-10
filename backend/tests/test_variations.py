"""Product variations: matched from what Etsy sent, and what they change.

A listing sells one product in several combinations, and those combinations are
not interchangeable — *Bin Fan: Yes* is a different plate from *Bin Fan: No*.
Getting this wrong is the quiet kind of wrong: the board looks entirely normal
and the wrong file goes on the printer. So these tests care most about which
variation a line lands on, and about what happens when none of them fit.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    BomLine,
    OrderLine,
    PrintJob,
    PrintMapping,
    Product,
    ProductVariation,
)
from app.services import intake, variations

pytestmark = pytest.mark.asyncio

LISTING = 1_895_497_697
VARIANT_NO = 26_682_511_648
VARIANT_YES = 26_682_511_649


def _line(**fields):
    """A detached order line — automatch is pure, so it needs nothing else."""
    return OrderLine(quantity=1, state="new", **fields)


def _variation(**fields):
    fields.setdefault("label", "x")
    fields.setdefault("options", [])
    fields.setdefault("active", True)
    return ProductVariation(**fields)


# --------------------------------------------------------------------------
# Choosing a variation
# --------------------------------------------------------------------------


class TestAutomatch:
    async def test_the_etsy_variation_id_wins(self):
        yes = _variation(etsy_product_id=VARIANT_YES, options=[{"name": "Bin Fan", "value": "Yes"}])
        no = _variation(etsy_product_id=VARIANT_NO, options=[{"name": "Bin Fan", "value": "No"}])
        line = _line(
            etsy_product_id=VARIANT_NO,
            variations=[{"name": "Bin Fan", "value": "Yes"}],
        )
        # The id and the options disagree — trust the id, it is Etsy's own
        # answer about which combination was sold.
        assert variations.automatch(line, [yes, no]) is no

    async def test_options_match_when_there_is_no_id(self):
        """Etsy reissues variation ids on every options edit; values survive."""
        yes = _variation(options=[{"name": "Bin Fan", "value": "Yes"}])
        no = _variation(options=[{"name": "Bin Fan", "value": "No"}])
        line = _line(variations=[{"name": "Bin Fan", "value": "Yes"}])
        assert variations.automatch(line, [no, yes]) is yes

    async def test_options_match_after_etsy_reissued_the_ids(self):
        yes = _variation(
            etsy_product_id=999_999, options=[{"name": "Bin Fan", "value": "Yes"}]
        )
        line = _line(
            etsy_product_id=VARIANT_YES,
            variations=[{"name": "Bin Fan", "value": "Yes"}],
        )
        assert variations.automatch(line, [yes]) is yes

    async def test_case_and_spacing_do_not_matter(self):
        """These strings are typed by hand in Etsy's listing editor."""
        yes = _variation(options=[{"name": "Bin Fan", "value": "Yes"}])
        line = _line(variations=[{"name": "  bin  fan ", "value": "YES"}])
        assert variations.automatch(line, [yes]) is yes

    async def test_a_variation_pinning_fewer_options_still_matches(self):
        """The variation cares about the fan; the buyer also picked a colour."""
        fan = _variation(options=[{"name": "Bin Fan", "value": "Yes"}])
        line = _line(
            variations=[
                {"name": "Bin Fan", "value": "Yes"},
                {"name": "Colour", "value": "Red"},
            ]
        )
        assert variations.automatch(line, [fan]) is fan

    async def test_a_variation_wanting_more_than_was_chosen_does_not_match(self):
        both = _variation(
            options=[
                {"name": "Bin Fan", "value": "Yes"},
                {"name": "Colour", "value": "Red"},
            ]
        )
        line = _line(variations=[{"name": "Bin Fan", "value": "Yes"}])
        assert variations.automatch(line, [both]) is None

    async def test_personalisation_never_stops_a_match(self):
        """Free text is one buyer's engraving, not a combination the shop sells."""
        yes = _variation(options=[{"name": "Bin Fan", "value": "Yes"}])
        line = _line(
            variations=[
                {"name": "Bin Fan", "value": "Yes"},
                {"name": "Personalization", "value": "For Ada", "free_text": True},
            ]
        )
        assert variations.automatch(line, [yes]) is yes

    async def test_a_retired_variation_is_ignored(self):
        old = _variation(
            etsy_product_id=VARIANT_YES,
            options=[{"name": "Bin Fan", "value": "Yes"}],
            active=False,
        )
        line = _line(etsy_product_id=VARIANT_YES)
        assert variations.automatch(line, [old]) is None

    async def test_no_options_and_no_id_matches_nothing(self):
        yes = _variation(options=[{"name": "Bin Fan", "value": "Yes"}])
        assert variations.automatch(_line(), [yes]) is None

    async def test_a_product_without_variations_matches_nothing(self):
        assert variations.automatch(_line(etsy_product_id=VARIANT_YES), []) is None


# --------------------------------------------------------------------------
# What a variation overrides
# --------------------------------------------------------------------------


def _mapping(**fields):
    fields.setdefault("bambuddy_archive_id", 10)
    fields.setdefault("plate_number", 1)
    fields.setdefault("units_per_plate", 4)
    # No printer models: this plate can go on anything.
    fields.setdefault("printer_models", [])
    return PrintMapping(**fields)


class TestPrintPlan:
    async def test_no_variation_uses_the_product_mapping(self):
        plan = variations.print_plan(_mapping(), None)
        assert (plan.bambuddy_archive_id, plan.plate_number, plan.units_per_plate) == (10, 1, 4)

    async def test_a_variation_archive_replaces_the_file(self):
        plan = variations.print_plan(
            _mapping(), _variation(bambuddy_archive_id=77, plate_number=2, units_per_plate=6)
        )
        assert (plan.bambuddy_archive_id, plan.plate_number, plan.units_per_plate) == (77, 2, 6)

    async def test_a_variation_can_adjust_the_plate_without_a_new_file(self):
        """Same 3MF, different plate on it — a common way to model an option."""
        plan = variations.print_plan(_mapping(), _variation(plate_number=3))
        assert plan.bambuddy_archive_id == 10
        assert plan.plate_number == 3
        assert plan.units_per_plate == 4

    async def test_a_variation_archive_stands_in_for_a_missing_mapping(self):
        plan = variations.print_plan(None, _variation(bambuddy_archive_id=77))
        assert plan.bambuddy_archive_id == 77
        # Nothing to inherit, so the safe defaults apply.
        assert (plan.plate_number, plan.units_per_plate) == (1, 1)

    async def test_nothing_to_print_from_is_reported_as_nothing(self):
        assert variations.print_plan(None, _variation()) is None
        assert variations.print_plan(None, None) is None


class TestStockItem:
    async def test_the_product_item_is_the_default(self):
        product = Product(sku="A", name="A", fulfillment="stocked", qbo_item_id="7")
        assert variations.stock_item(product, None) == ("7", None)

    async def test_a_variation_item_takes_over(self):
        product = Product(sku="A", name="A", fulfillment="stocked", qbo_item_id="7")
        variation = _variation(qbo_item_id="9", qbo_item_name="Red")
        assert variations.stock_item(product, variation) == ("9", "Red")

    async def test_a_variation_without_an_item_falls_back(self):
        product = Product(sku="A", name="A", fulfillment="stocked", qbo_item_id="7")
        assert variations.stock_item(product, _variation()) == ("7", None)


# --------------------------------------------------------------------------
# Reading them off a listing
# --------------------------------------------------------------------------


class TestFromEtsy:
    async def test_each_combination_becomes_a_row(self):
        rows = variations.from_etsy_variants(
            [
                {"product_id": VARIANT_NO, "options": [{"name": "Bin Fan", "value": "No"}]},
                {"product_id": VARIANT_YES, "options": [{"name": "Bin Fan", "value": "Yes"}]},
            ],
            LISTING,
        )
        assert [row["label"] for row in rows] == ["Bin Fan: No", "Bin Fan: Yes"]
        assert [row["etsy_product_id"] for row in rows] == [VARIANT_NO, VARIANT_YES]
        assert {row["etsy_listing_id"] for row in rows} == {LISTING}

    async def test_a_listing_without_options_yields_nothing(self):
        """No options means no variations — one product covers it."""
        assert variations.from_etsy_variants([{"product_id": 1, "options": []}], LISTING) == []

    async def test_duplicate_combinations_collapse(self):
        rows = variations.from_etsy_variants(
            [
                {"product_id": 1, "options": [{"name": "Colour", "value": "Red"}]},
                {"product_id": 2, "options": [{"name": "colour", "value": "red"}]},
            ],
            LISTING,
        )
        assert len(rows) == 1

    async def test_several_options_make_one_combination(self):
        rows = variations.from_etsy_variants(
            [
                {
                    "product_id": 1,
                    "options": [
                        {"name": "Bin Fan", "value": "Yes"},
                        {"name": "Colour", "value": "Red"},
                    ],
                }
            ],
            LISTING,
        )
        assert rows[0]["label"] == "Bin Fan: Yes · Colour: Red"


# --------------------------------------------------------------------------
# End to end, through intake
# --------------------------------------------------------------------------


def _receipt(receipt_id, *, value, product_id):
    return {
        "receipt_id": receipt_id,
        "name": "Ada",
        "created_timestamp": 1_780_000_000,
        "transactions": [
            {
                "transaction_id": receipt_id * 10,
                "listing_id": LISTING,
                "product_id": product_id,
                "sku": "BIN",
                "title": "Storage bin",
                "quantity": 1,
                "variations": [{"formatted_name": "Bin Fan", "formatted_value": value}],
            }
        ],
    }


async def _setup(db, *, yes_archive=77):
    product = Product(sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None)
    db.add(product)
    await db.flush()
    db.add(
        PrintMapping(
            product_id=product.id,
            bambuddy_archive_id=10,
            plate_number=1,
            units_per_plate=1,
        )
    )
    db.add(
        ProductVariation(
            product_id=product.id,
            label="Bin Fan: No",
            options=[{"name": "Bin Fan", "value": "No"}],
            etsy_listing_id=LISTING,
            etsy_product_id=VARIANT_NO,
        )
    )
    db.add(
        ProductVariation(
            product_id=product.id,
            label="Bin Fan: Yes",
            options=[{"name": "Bin Fan", "value": "Yes"}],
            etsy_listing_id=LISTING,
            etsy_product_id=VARIANT_YES,
            bambuddy_archive_id=yes_archive,
            plate_number=2,
        )
    )
    await db.commit()
    return product


async def _first_line(db, order):
    return (
        await db.execute(
            select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )
    ).scalar_one()


class TestThroughIntake:
    async def test_the_line_records_which_variation_it_is(self, db):
        await _setup(db)
        order, _ = await intake.ingest_receipt(
            db, _receipt(1, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        line = await _first_line(db, order)
        variation = await db.get(ProductVariation, line.variation_id)
        assert variation.label == "Bin Fan: Yes"

    async def test_the_right_plate_is_queued(self, db):
        """The whole point: two variations, two different things on the printer."""
        await _setup(db)
        order, _ = await intake.ingest_receipt(
            db, _receipt(2, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        line = await _first_line(db, order)
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        assert job.bambuddy_archive_id == 77
        assert job.plate_number == 2

    async def test_the_other_variation_uses_the_product_mapping(self, db):
        await _setup(db)
        order, _ = await intake.ingest_receipt(
            db, _receipt(3, value="No", product_id=VARIANT_NO)
        )
        await db.commit()
        line = await _first_line(db, order)
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        assert job.bambuddy_archive_id == 10
        assert job.plate_number == 1

    async def test_an_option_nobody_set_up_still_flows_through(self, db):
        """A combination with no variation must not strand the order."""
        await _setup(db)
        order, _ = await intake.ingest_receipt(
            db, _receipt(4, value="Maybe", product_id=999)
        )
        await db.commit()
        line = await _first_line(db, order)
        assert line.variation_id is None
        assert line.state != "unmatched"
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        assert job.bambuddy_archive_id == 10

    async def test_adding_a_variation_later_is_picked_up_by_reprocessing(self, db):
        """Variations get set up after the first order arrives, not before."""
        product = Product(
            sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None
        )
        db.add(product)
        await db.flush()
        db.add(
            PrintMapping(
                product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(5, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        assert (await _first_line(db, order)).variation_id is None

        db.add(
            ProductVariation(
                product_id=product.id,
                label="Bin Fan: Yes",
                options=[{"name": "Bin Fan", "value": "Yes"}],
                etsy_product_id=VARIANT_YES,
            )
        )
        await db.commit()

        await intake.process_order(db, order)
        await db.commit()
        line = await _first_line(db, order)
        assert line.variation_id is not None

    async def test_a_pending_job_is_corrected_when_the_variation_arrives(self, db):
        """The queued plate has to change too, or the fix is cosmetic.

        Only a job that has not reached Bambuddy is rewritten — one already on
        a printer is a fact, and deleting the row would not unprint it.
        """
        product = Product(
            sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None
        )
        db.add(product)
        await db.flush()
        db.add(
            PrintMapping(
                product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(6, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        line = await _first_line(db, order)
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        assert job.bambuddy_archive_id == 10

        db.add(
            ProductVariation(
                product_id=product.id,
                label="Bin Fan: Yes",
                options=[{"name": "Bin Fan", "value": "Yes"}],
                etsy_product_id=VARIANT_YES,
                bambuddy_archive_id=77,
                plate_number=2,
            )
        )
        await db.commit()

        await intake.process_order(db, order)
        await db.commit()
        jobs = (
            (await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id)))
            .scalars()
            .all()
        )
        assert [(j.bambuddy_archive_id, j.plate_number) for j in jobs] == [(77, 2)]

    async def test_a_dispatched_job_is_left_alone(self, db):
        """It is already on a printer; the row is a record, not an intention."""
        product = Product(
            sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None
        )
        db.add(product)
        await db.flush()
        db.add(
            PrintMapping(
                product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(7, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        line = await _first_line(db, order)
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        job.status = "queued"
        job.bambuddy_queue_id = 555
        db.add(
            ProductVariation(
                product_id=product.id,
                label="Bin Fan: Yes",
                options=[{"name": "Bin Fan", "value": "Yes"}],
                etsy_product_id=VARIANT_YES,
                bambuddy_archive_id=77,
            )
        )
        await db.commit()

        await intake.process_order(db, order)
        await db.commit()
        jobs = (
            (await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id)))
            .scalars()
            .all()
        )
        assert [j.bambuddy_archive_id for j in jobs] == [10]


# --------------------------------------------------------------------------
# Stock, when variations track separately
# --------------------------------------------------------------------------


class TestStockPerVariation:
    """Reservations follow the QuickBooks item, which a variation can change.

    Both halves matter. Separating variations that track their own stock is the
    new behaviour; still pooling the ones that share an item is the behaviour
    that was already there and must not have been broken to get it.
    """

    @staticmethod
    def _qbo(monkeypatch, on_hand=1):
        class Qbo:
            async def get_items(self, item_ids):
                return {
                    str(i): {"Id": str(i), "TrackQtyOnHand": True, "QtyOnHand": on_hand}
                    for i in item_ids
                }

        async def client_for(_session):
            return Qbo()

        monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)

    @staticmethod
    async def _shop(db, items):
        product = Product(sku="BIN", name="Bin", fulfillment="stocked", qbo_item_id="7")
        db.add(product)
        await db.flush()
        for value, item in items:
            db.add(
                ProductVariation(
                    product_id=product.id,
                    label=f"Colour: {value}",
                    options=[{"name": "Colour", "value": value}],
                    qbo_item_id=item,
                )
            )
        await db.commit()
        return product

    @staticmethod
    async def _order(db, index, value):
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 100 + index,
                "transactions": [
                    {
                        "transaction_id": 1000 + index,
                        "listing_id": LISTING,
                        "sku": "BIN",
                        "quantity": 1,
                        "variations": [
                            {"formatted_name": "Colour", "formatted_value": value}
                        ],
                    }
                ],
            },
        )
        await db.commit()

    async def test_variations_with_their_own_items_do_not_compete(self, db, monkeypatch):
        """Red and blue are different stock; one must not eat the other's."""
        self._qbo(monkeypatch, on_hand=1)
        await self._shop(db, [("Red", "70"), ("Blue", "71")])
        for index, value in enumerate(("Red", "Blue")):
            await self._order(db, index, value)

        lines = (await db.execute(select(OrderLine))).scalars().all()
        assert [line.qty_from_stock for line in lines] == [1, 1]
        assert [line.stock_note for line in lines] == [None, None]

    async def test_variations_sharing_an_item_still_compete(self, db, monkeypatch):
        """One unit between them: the second order has to be told it is short."""
        self._qbo(monkeypatch, on_hand=1)
        await self._shop(db, [("Red", None), ("Blue", None)])
        for index, value in enumerate(("Red", "Blue")):
            await self._order(db, index, value)

        notes = [
            line.stock_note
            for line in (await db.execute(select(OrderLine))).scalars().all()
        ]
        assert notes[0] is None
        assert "Short on stock: 0 available" in notes[1]


# --------------------------------------------------------------------------
# Variants as products of their own
# --------------------------------------------------------------------------


class TestVariantProducts:
    """A variation can be a product, and then it brings its own components.

    "With fan" and "without fan" are not one build with a substitution; they
    are two builds. The master is what Etsy sells and what an order matches
    first — the variant is what actually gets made.
    """

    @staticmethod
    async def _family(db):
        master = Product(
            sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None
        )
        db.add(master)
        await db.flush()
        db.add(
            PrintMapping(
                product_id=master.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        variation = ProductVariation(
            product_id=master.id,
            label="Bin Fan: Yes",
            options=[{"name": "Bin Fan", "value": "Yes"}],
            etsy_product_id=VARIANT_YES,
        )
        db.add(variation)
        await db.flush()
        return master, variation

    async def test_the_line_becomes_the_variant_product(self, db):
        master, variation = await self._family(db)
        variant = Product(
            sku="BIN-FAN", name="Bin, with fan", parent_id=master.id,
            fulfillment="printed", qbo_item_id=None,
        )
        db.add(variant)
        await db.flush()
        db.add(
            PrintMapping(
                product_id=variant.id, bambuddy_archive_id=99, plate_number=1, units_per_plate=1
            )
        )
        variation.variant_product_id = variant.id
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(20, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        line = await _first_line(db, order)
        assert line.product_id == variant.id
        job = (
            await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id))
        ).scalar_one()
        assert job.bambuddy_archive_id == 99

    async def test_a_variant_bundle_explodes_into_its_own_components(self, db):
        """The whole point of the change: different components per variant."""
        master, variation = await self._family(db)
        body = Product(sku="BODY", name="Body", fulfillment="printed", qbo_item_id=None)
        fan = Product(sku="FAN", name="Fan", fulfillment="stocked", qbo_item_id=None)
        variant = Product(
            sku="BIN-FAN", name="Bin, with fan", parent_id=master.id,
            fulfillment="bundle", qbo_item_id=None,
        )
        db.add_all([body, fan, variant])
        await db.flush()
        db.add(BomLine(bundle_id=variant.id, component_id=body.id, quantity=1))
        db.add(BomLine(bundle_id=variant.id, component_id=fan.id, quantity=2))
        variation.variant_product_id = variant.id
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(21, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()

        children = (
            (
                await db.execute(
                    select(OrderLine).where(OrderLine.parent_line_id.isnot(None))
                )
            )
            .scalars()
            .all()
        )
        assert sorted((c.sku_raw, c.quantity) for c in children) == [
            ("BODY", 1),
            ("FAN", 2),
        ]

    async def test_the_other_variation_still_uses_the_master(self, db):
        master, variation = await self._family(db)
        db.add(
            ProductVariation(
                product_id=master.id,
                label="Bin Fan: No",
                options=[{"name": "Bin Fan", "value": "No"}],
                etsy_product_id=VARIANT_NO,
            )
        )
        variant = Product(
            sku="BIN-FAN", name="Bin, with fan", parent_id=master.id,
            fulfillment="printed", qbo_item_id=None,
        )
        db.add(variant)
        await db.flush()
        variation.variant_product_id = variant.id
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(22, value="No", product_id=VARIANT_NO)
        )
        await db.commit()
        assert (await _first_line(db, order)).product_id == master.id

    async def test_re_running_intake_does_not_lose_the_variation(self, db):
        """The line now points at the variant, whose variations live on the master."""
        master, variation = await self._family(db)
        variant = Product(
            sku="BIN-FAN", name="Bin, with fan", parent_id=master.id,
            fulfillment="printed", qbo_item_id=None,
        )
        db.add(variant)
        await db.flush()
        variation.variant_product_id = variant.id
        await db.commit()

        order, _ = await intake.ingest_receipt(
            db, _receipt(23, value="Yes", product_id=VARIANT_YES)
        )
        await db.commit()
        await intake.process_order(db, order)
        await db.commit()

        line = await _first_line(db, order)
        assert line.product_id == variant.id
        assert line.variation_id == variation.id


class TestVariantProductApi:
    async def test_creating_one_nests_it_under_the_master(self, signed_in, db):
        master = Product(sku="BIN", name="Storage bin", fulfillment="printed", qbo_item_id=None)
        db.add(master)
        await db.flush()
        variation = ProductVariation(
            product_id=master.id,
            label="Bin Fan: Yes",
            options=[{"name": "Bin Fan", "value": "Yes"}],
            etsy_listing_id=LISTING,
            etsy_product_id=VARIANT_YES,
        )
        db.add(variation)
        await db.commit()

        response = await signed_in.post(
            f"/api/products/{master.id}/variations/{variation.id}/product",
            json={"fulfillment": "bundle"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["variations"][0]["variant_product_name"] == "Storage bin — Bin Fan: Yes"

        listing = (await signed_in.get("/api/products")).json()["products"]
        variant = next(p for p in listing if p["parent_id"] == str(master.id))
        assert variant["fulfillment"] == "bundle"
        assert variant["sku"] == f"ETSY-{LISTING}-{VARIANT_YES}"

    async def test_a_variant_cannot_have_variants_of_its_own(self, signed_in, db):
        master = Product(sku="BIN", name="Bin", fulfillment="printed", qbo_item_id=None)
        db.add(master)
        await db.flush()
        variant = Product(
            sku="BIN-FAN", name="Bin, fan", parent_id=master.id,
            fulfillment="printed", qbo_item_id=None,
        )
        db.add(variant)
        await db.flush()
        nested = ProductVariation(
            product_id=variant.id, label="Colour: Red",
            options=[{"name": "Colour", "value": "Red"}],
        )
        db.add(nested)
        await db.commit()

        response = await signed_in.post(
            f"/api/products/{variant.id}/variations/{nested.id}/product",
            json={"fulfillment": "printed"},
        )
        assert response.status_code == 400
        assert "do not nest" in response.json()["detail"]

    async def test_a_master_with_variants_is_not_deleted_by_accident(self, signed_in, db):
        master = Product(sku="BIN", name="Bin", fulfillment="printed", qbo_item_id=None)
        db.add(master)
        await db.flush()
        db.add(
            Product(
                sku="BIN-FAN", name="Bin, fan", parent_id=master.id,
                fulfillment="printed", qbo_item_id=None,
            )
        )
        await db.commit()

        response = await signed_in.delete(f"/api/products/{master.id}")
        assert response.status_code == 409
        assert "1 variant" in response.json()["detail"]

    async def test_a_whole_family_can_be_deleted_together(self, signed_in, db):
        """Selecting both must work, whichever order they arrive in."""
        master = Product(sku="BIN", name="Bin", fulfillment="printed", qbo_item_id=None)
        db.add(master)
        await db.flush()
        variant = Product(
            sku="BIN-FAN", name="Bin, fan", parent_id=master.id,
            fulfillment="printed", qbo_item_id=None,
        )
        db.add(variant)
        await db.flush()
        db.add(
            ProductVariation(
                product_id=master.id, label="Bin Fan: Yes",
                options=[{"name": "Bin Fan", "value": "Yes"}],
                variant_product_id=variant.id,
            )
        )
        await db.commit()

        # Master listed first, which is the order the screen would send.
        response = await signed_in.post(
            "/api/products/bulk-delete",
            json={"product_ids": [str(master.id), str(variant.id)]},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": 2, "kept": []}
        assert (await signed_in.get("/api/products")).json()["products"] == []
