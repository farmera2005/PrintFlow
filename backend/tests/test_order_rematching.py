"""Seeing every order, and being able to change a match that was wrong.

A match used to be one-way: once a line found a product there was no route
back, so an order matched to the wrong thing stayed that way. And the board
only draws live columns, so a cancelled order was not reachable at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import BomLine, OrderLine, PrintJob, PrintMapping, Product
from app.services import intake

pytestmark = pytest.mark.asyncio

LISTING = 1_895_497_697


def _receipt(receipt_id, *, sku="BIN", quantity=1):
    return {
        "receipt_id": receipt_id,
        "name": "Ada",
        "created_timestamp": 1_780_000_000,
        "transactions": [
            {
                "transaction_id": receipt_id * 10,
                "listing_id": LISTING,
                "sku": sku,
                "title": "Storage bin",
                "quantity": quantity,
            }
        ],
    }


async def _product(db, sku, fulfillment="printed", **fields):
    product = Product(sku=sku, name=sku, fulfillment=fulfillment, qbo_item_id=None, **fields)
    db.add(product)
    await db.flush()
    return product


async def _top_line(db, order):
    return (
        await db.execute(
            select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# Listing every order
# --------------------------------------------------------------------------


class TestListingOrders:
    async def test_a_cancelled_order_is_still_reachable(self, signed_in, db):
        """It is invisible on the board, which is exactly why this screen exists."""
        await _product(db, "BIN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(1))
        order.status = "cancelled"
        await db.commit()

        body = (await signed_in.get("/api/orders")).json()
        assert [o["order_number"] for o in body["orders"]] == ["1"]
        assert body["counts"]["cancelled"] == 1
        assert body["total"] == 1

    async def test_it_can_be_filtered_to_one_status(self, signed_in, db):
        await _product(db, "BIN")
        await db.commit()
        first, _ = await intake.ingest_receipt(db, _receipt(2))
        second, _ = await intake.ingest_receipt(db, _receipt(3))
        first.status = "cancelled"
        await db.commit()

        body = (await signed_in.get("/api/orders?status_filter=cancelled")).json()
        assert [o["order_number"] for o in body["orders"]] == ["2"]
        # Counts describe everything, not the filtered slice — otherwise the
        # tabs would empty themselves as soon as one was chosen.
        assert body["counts"]["cancelled"] == 1
        assert body["total"] == 2

    async def test_a_nonsense_status_is_refused(self, signed_in):
        response = await signed_in.get("/api/orders?status_filter=elsewhere")
        assert response.status_code == 400

    async def test_search_finds_by_buyer(self, signed_in, db):
        await _product(db, "BIN")
        await db.commit()
        await intake.ingest_receipt(db, _receipt(4))
        await db.commit()
        body = (await signed_in.get("/api/orders?q=ada")).json()
        assert len(body["orders"]) == 1


# --------------------------------------------------------------------------
# Letting go of a match
# --------------------------------------------------------------------------


class TestUnmatching:
    async def test_a_line_can_be_released_and_pointed_elsewhere(self, signed_in, db):
        wrong = await _product(db, "BIN")
        right = await _product(db, "BIN-CORRECT")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(10))
        await db.commit()
        line = await _top_line(db, order)
        assert line.product_id == wrong.id

        released = await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/unmatch"
        )
        assert released.status_code == 200
        await db.refresh(line)
        assert line.product_id is None
        assert line.state == "unmatched"

        await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/link-product",
            json={"product_id": str(right.id)},
        )
        await db.refresh(line)
        assert line.product_id == right.id

    async def test_undispatched_plates_go_with_it(self, signed_in, db):
        product = await _product(db, "BIN")
        await db.flush()
        db.add(
            PrintMapping(
                product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(11))
        await db.commit()
        line = await _top_line(db, order)
        assert len((await db.execute(select(PrintJob))).scalars().all()) == 1

        body = (
            await signed_in.post(f"/api/orders/{order.id}/lines/{line.id}/unmatch")
        ).json()
        assert body["kept_jobs"] == 0
        assert (await db.execute(select(PrintJob))).scalars().all() == []

    async def test_a_plate_already_on_a_printer_is_kept_and_reported(self, signed_in, db):
        """Forgetting the row here would not unprint it."""
        product = await _product(db, "BIN")
        await db.flush()
        db.add(
            PrintMapping(
                product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
            )
        )
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(12))
        await db.commit()
        line = await _top_line(db, order)
        job = (await db.execute(select(PrintJob))).scalar_one()
        job.status = "printing"
        job.bambuddy_queue_id = 42
        await db.commit()

        body = (
            await signed_in.post(f"/api/orders/{order.id}/lines/{line.id}/unmatch")
        ).json()
        assert body["kept_jobs"] == 1
        assert len((await db.execute(select(PrintJob))).scalars().all()) == 1

    async def test_a_bundle_takes_its_components_with_it(self, signed_in, db):
        bundle = await _product(db, "BIN", fulfillment="bundle")
        part = await _product(db, "PART", fulfillment="stocked")
        db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=2))
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(13))
        await db.commit()
        assert len((await db.execute(select(OrderLine))).scalars().all()) == 2

        line = await _top_line(db, order)
        body = (
            await signed_in.post(f"/api/orders/{order.id}/lines/{line.id}/unmatch")
        ).json()
        assert body["removed_children"] == 1
        assert len((await db.execute(select(OrderLine))).scalars().all()) == 1

    async def test_a_component_cannot_be_unmatched_on_its_own(self, signed_in, db):
        """It exists because of its bundle; releasing it alone means nothing."""
        bundle = await _product(db, "BIN", fulfillment="bundle")
        part = await _product(db, "PART", fulfillment="stocked")
        db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=1))
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(14))
        await db.commit()
        child = (
            await db.execute(
                select(OrderLine).where(OrderLine.parent_line_id.isnot(None))
            )
        ).scalar_one()

        response = await signed_in.post(
            f"/api/orders/{order.id}/lines/{child.id}/unmatch"
        )
        assert response.status_code == 400
        assert "bundle component" in response.json()["detail"]


# --------------------------------------------------------------------------
# Resetting a whole order
# --------------------------------------------------------------------------


class TestResetMatching:
    async def test_it_picks_up_a_catalogue_fixed_after_the_order_arrived(
        self, signed_in, db
    ):
        """The reason this exists: the order stalled, then the shop was fixed."""
        order, _ = await intake.ingest_receipt(db, _receipt(20, sku="UNKNOWN"))
        await db.commit()
        assert (await _top_line(db, order)).state == "unmatched"

        product = await _product(db, "UNKNOWN")
        await db.commit()

        body = (await signed_in.post(f"/api/orders/{order.id}/reset-matching")).json()
        assert body["lines"] == 1
        line = await _top_line(db, order)
        await db.refresh(line)
        assert line.product_id == product.id

    async def test_it_drops_a_match_that_is_no_longer_right(self, signed_in, db):
        """Plain re-run keeps what a line already has; reset does not."""
        first = await _product(db, "BIN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(21))
        await db.commit()
        line = await _top_line(db, order)
        assert line.product_id == first.id

        # The shop renames the product's code, so nothing matches any more.
        first.sku = "BIN-OLD"
        await db.commit()

        await signed_in.post(f"/api/orders/{order.id}/reprocess")
        await db.refresh(line)
        assert line.product_id == first.id, "a plain re-run keeps the existing match"

        await signed_in.post(f"/api/orders/{order.id}/reset-matching")
        await db.refresh(line)
        assert line.product_id is None
        assert line.state == "unmatched"
