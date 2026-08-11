"""End-to-end intake: SKU match → bundle explosion → decisioning → plate planning."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (
    BomLine,
    OrderLine,
    PrintJob,
    PrintFile,
    Product,
)
from app.services import intake, printing, state
from app.services.state import recompute_order

pytestmark = pytest.mark.asyncio


async def suggests(db, order):
    """Where the rules would put this order, with its lines loaded to say so."""
    lines = (
        (
            await db.execute(
                select(OrderLine)
                .where(OrderLine.order_id == order.id)
                .options(selectinload(OrderLine.print_jobs))
            )
        )
        .scalars()
        .all()
    )
    return state.suggested_status(order, list(lines))


class FakeQbo:
    """Stands in for the QuickBooks client with a fixed set of items."""

    def __init__(self, quantities: dict[str, float]):
        self.quantities = quantities
        self.calls: list[list[str]] = []

    async def get_items(self, item_ids):
        self.calls.append(list(item_ids))
        return {
            item_id: {"Id": item_id, "TrackQtyOnHand": True, "QtyOnHand": qty}
            for item_id, qty in self.quantities.items()
            if item_id in {str(i) for i in item_ids}
        }


class FakeBambuddy:
    def __init__(self):
        self.enqueued: list[dict] = []
        self.next_id = 1000
        self.queue: list[dict] = []

    async def enqueue(
        self,
        *,
        archive_id=None,
        plate_number=None,
        printer_id=None,
        print_options=None,
        file_path=None,
    ):
        self.next_id += 1
        self.enqueued.append(
            {
                "archive_id": archive_id,
                "file_path": file_path,
                "plate_number": plate_number,
                "printer_id": printer_id,
                "print_options": print_options,
            }
        )
        item = {"id": self.next_id, "status": "queued", "archive_id": archive_id}
        self.queue.append(item)
        return item

    async def list_queue(self):
        return list(self.queue)

    async def cancel(self, queue_id):
        self.queue = [item for item in self.queue if item["id"] != queue_id]


@pytest.fixture
def fake_qbo(monkeypatch):
    fake = FakeQbo({"QBO-X": 4, "QBO-T": 10})

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)
    return fake


@pytest.fixture
def fake_bambuddy(monkeypatch):
    fake = FakeBambuddy()

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.printing.bambuddy_api.client_for", client_for)
    return fake


async def seed_catalog(db) -> dict[str, Product]:
    bundle = Product(sku="BUNDLE-A", name="Gift set", fulfillment="bundle")
    part_x = Product(
        sku="PART-X", name="Printed part X", fulfillment="printed", qbo_item_id="QBO-X"
    )
    part_y = Product(sku="PART-Y", name="Printed part Y", fulfillment="printed")
    trinket = Product(
        sku="TRINKET", name="Stocked trinket", fulfillment="stocked", qbo_item_id="QBO-T"
    )
    db.add_all([bundle, part_x, part_y, trinket])
    await db.flush()

    db.add_all(
        [
            BomLine(bundle_id=bundle.id, component_id=part_x.id, quantity=2),
            BomLine(bundle_id=bundle.id, component_id=part_y.id, quantity=1),
            # PART-X yields four units per plate; PART-Y one.
            PrintFile(
                product_id=part_x.id,
                bambuddy_archive_id=501,
                plate_number=2,
                units_per_plate=4,
                print_options={"filament": "PLA-black"},
            ),
            PrintFile(
                product_id=part_y.id, bambuddy_archive_id=502, units_per_plate=1
            ),
        ]
    )
    await db.commit()
    return {"bundle": bundle, "part_x": part_x, "part_y": part_y, "trinket": trinket}


def receipt(**overrides) -> dict:
    base = {
        "receipt_id": 900123,
        "name": "Dana Buyer",
        "created_timestamp": 1_760_000_000,
        "transactions": [
            {"transaction_id": 1, "listing_id": 11, "sku": " bundle-a ", "quantity": 3},
            {"transaction_id": 2, "listing_id": 12, "sku": "TRINKET", "quantity": 2},
            {"transaction_id": 3, "listing_id": 13, "sku": "MYSTERY-9", "quantity": 1},
        ],
    }
    base.update(overrides)
    return base


async def lines_by_sku(db, order_id) -> dict[str, OrderLine]:
    rows = (
        (await db.execute(select(OrderLine).where(OrderLine.order_id == order_id)))
        .scalars()
        .all()
    )
    out: dict[str, OrderLine] = {}
    for line in rows:
        key = line.sku_raw or "?"
        out[key.strip()] = line
    return out


class TestIngest:
    async def test_bundle_explodes_into_components(self, db, fake_qbo):
        await seed_catalog(db)
        order, created = await intake.ingest_receipt(db, receipt())
        await db.commit()

        assert created is True
        assert order.order_number == "900123"
        assert order.buyer_name == "Dana Buyer"

        by_sku = await lines_by_sku(db, order.id)
        # SKU matching is case-insensitive and trims whitespace.
        bundle_line = by_sku["bundle-a"]
        assert bundle_line.state == "exploded"

        children = (
            (
                await db.execute(
                    select(OrderLine).where(OrderLine.parent_line_id == bundle_line.id)
                )
            )
            .scalars()
            .all()
        )
        quantities = {line.sku_raw: line.quantity for line in children}
        # 3 bundles × (2 PART-X + 1 PART-Y)
        assert quantities == {"PART-X": 6, "PART-Y": 3}

    async def test_unmatched_sku_is_flagged_not_dropped(self, db, fake_qbo):
        await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        by_sku = await lines_by_sku(db, order.id)
        assert by_sku["MYSTERY-9"].state == "unmatched"
        assert by_sku["MYSTERY-9"].product_id is None
        assert order.status == "new"

    async def test_stock_and_print_split_uses_qbo(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        components = (
            (
                await db.execute(
                    select(OrderLine).where(OrderLine.product_id.in_(
                        [catalog["part_x"].id, catalog["part_y"].id]
                    ))
                )
            )
            .scalars()
            .all()
        )
        by_product = {line.product_id: line for line in components}

        # PART-X: 6 needed, 4 on hand in QuickBooks → 4 pulled, 2 printed.
        part_x = by_product[catalog["part_x"].id]
        assert (part_x.qty_from_stock, part_x.qty_to_print) == (4, 2)

        # PART-Y has no QuickBooks item, so the stock check is skipped entirely.
        part_y = by_product[catalog["part_y"].id]
        assert (part_y.qty_from_stock, part_y.qty_to_print) == (0, 3)

    async def test_plates_are_planned_from_units_per_plate(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        jobs = (
            (
                await db.execute(
                    select(PrintJob)
                    .join(OrderLine, OrderLine.id == PrintJob.order_line_id)
                    .where(OrderLine.order_id == order.id)
                )
            )
            .scalars()
            .all()
        )
        by_archive: dict[int, list[PrintJob]] = {}
        for job in jobs:
            by_archive.setdefault(job.bambuddy_archive_id, []).append(job)

        # PART-X: 2 to print at 4 per plate → 1 plate. PART-Y: 3 at 1 each → 3 plates.
        assert len(by_archive[501]) == 1
        assert len(by_archive[502]) == 3
        assert by_archive[501][0].plate_number == 2
        assert all(job.status == "pending" for job in jobs)
        assert catalog["part_x"].id is not None

    async def test_stocked_line_is_allocated_never_printed(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        by_sku = await lines_by_sku(db, order.id)
        trinket = by_sku["TRINKET"]
        assert trinket.product_id == catalog["trinket"].id
        assert (trinket.qty_from_stock, trinket.qty_to_print) == (2, 0)
        assert trinket.state == "ready"

    async def test_reingesting_the_same_receipt_is_a_no_op(self, db, fake_qbo):
        await seed_catalog(db)
        order, created_first = await intake.ingest_receipt(db, receipt())
        await db.commit()
        first_line_count = len(
            (await db.execute(select(OrderLine).where(OrderLine.order_id == order.id)))
            .scalars()
            .all()
        )
        first_job_count = len((await db.execute(select(PrintJob))).scalars().all())

        _, created_again = await intake.ingest_receipt(db, receipt())
        await db.commit()

        assert created_first is True
        assert created_again is False
        assert (
            len(
                (
                    await db.execute(
                        select(OrderLine).where(OrderLine.order_id == order.id)
                    )
                )
                .scalars()
                .all()
            )
            == first_line_count
        )
        assert len((await db.execute(select(PrintJob))).scalars().all()) == first_job_count

    async def test_reprocessing_does_not_duplicate_plates(self, db, fake_qbo):
        await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()
        before = len((await db.execute(select(PrintJob))).scalars().all())

        await intake.process_order(db, order)
        await db.commit()

        assert len((await db.execute(select(PrintJob))).scalars().all()) == before


class TestSoftReservations:
    async def test_a_second_order_cannot_claim_reserved_stock(self, db, fake_qbo):
        catalog = await seed_catalog(db)

        first = {
            "receipt_id": 1,
            "name": "First",
            "transactions": [{"transaction_id": 1, "sku": "PART-X", "quantity": 3}],
        }
        second = {
            "receipt_id": 2,
            "name": "Second",
            "transactions": [{"transaction_id": 2, "sku": "PART-X", "quantity": 3}],
        }

        order_one, _ = await intake.ingest_receipt(db, first)
        await db.commit()
        order_two, _ = await intake.ingest_receipt(db, second)
        await db.commit()

        lines_one = await lines_by_sku(db, order_one.id)
        lines_two = await lines_by_sku(db, order_two.id)

        # Only 4 units on hand: the first order takes 3, the second gets 1.
        assert (lines_one["PART-X"].qty_from_stock, lines_one["PART-X"].qty_to_print) == (3, 0)
        assert (lines_two["PART-X"].qty_from_stock, lines_two["PART-X"].qty_to_print) == (1, 2)
        assert catalog["part_x"].qbo_item_id == "QBO-X"

    async def test_cancelling_a_line_releases_its_reservation(self, db, fake_qbo):
        await seed_catalog(db)
        order_one, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 1,
                "transactions": [{"transaction_id": 1, "sku": "PART-X", "quantity": 4}],
            },
        )
        await db.commit()

        lines = await lines_by_sku(db, order_one.id)
        lines["PART-X"].override_state = "cancelled"
        lines["PART-X"].state = "cancelled"
        await db.commit()

        order_two, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 2,
                "transactions": [{"transaction_id": 2, "sku": "PART-X", "quantity": 4}],
            },
        )
        await db.commit()

        released = await lines_by_sku(db, order_two.id)
        assert released["PART-X"].qty_from_stock == 4


class TestQboUnavailable:
    async def test_printed_lines_fall_back_to_printing_everything(self, db, monkeypatch):
        from app.services.credentials import IntegrationNotConfigured

        async def unavailable(_session):
            raise IntegrationNotConfigured("qbo")

        monkeypatch.setattr("app.services.allocation.qbo_api.client_for", unavailable)
        await seed_catalog(db)

        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 42,
                "transactions": [{"transaction_id": 1, "sku": "PART-X", "quantity": 5}],
            },
        )
        await db.commit()

        line = (await lines_by_sku(db, order.id))["PART-X"]
        assert (line.qty_from_stock, line.qty_to_print) == (0, 5)
        assert "QuickBooks is not connected" in (line.stock_note or "")


class TestProgressToReadyToShip:
    """The work advances on its own; the card does not.

    Line states are still derived from end to end — matched, allocated,
    printing, printed, ready — which is what tells an operator where the work
    actually is. The order's column is theirs to set, so it stays put until
    they move it.
    """

    async def test_full_lifecycle_through_assembly(self, db, fake_qbo, fake_bambuddy):
        await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        # Resolve the unmatched line so it stops holding the order back.
        by_sku = await lines_by_sku(db, order.id)
        mystery = by_sku["MYSTERY-9"]
        mystery.override_state = "cancelled"
        mystery.state = "cancelled"
        await db.flush()
        await recompute_order(db, order)
        await db.commit()

        # Push the planned plates to Bambuddy.
        stats = await printing.dispatch_pending(db)
        await db.commit()
        assert stats["dispatched"] == 4
        # The plates went out; the card has not moved.
        assert order.status == "new"
        # print_options from the mapping ride along with the queue request.
        # Found by archive rather than by position: every plate for this order is
        # created in one transaction, so they share a created_at and there is no
        # order between them to rely on.
        part_x_jobs = [job for job in fake_bambuddy.enqueued if job["archive_id"] == 501]
        assert part_x_jobs, fake_bambuddy.enqueued
        assert all(
            job["print_options"] == {"filament": "PLA-black"} for job in part_x_jobs
        )

        # Bambuddy reports everything finished.
        for item in fake_bambuddy.queue:
            item["status"] = "finished"
        await printing.reconcile(db)
        await db.commit()

        await db.refresh(order)
        by_sku = await lines_by_sku(db, order.id)
        # Components are printed, but the bundle still needs assembling — which
        # is exactly what the rules would say, without saying it for anyone.
        assert {by_sku["PART-X"].state, by_sku["PART-Y"].state} == {"printed"}
        assert await suggests(db, order) == "assembly"
        assert order.status == "new"

        bundle_line = (await lines_by_sku(db, order.id))["bundle-a"]
        from datetime import datetime, timezone

        bundle_line.assembled_at = datetime.now(timezone.utc)
        await db.flush()
        await recompute_order(db, order)
        await db.commit()

        by_sku = await lines_by_sku(db, order.id)
        assert by_sku["PART-X"].state == "ready"
        assert await suggests(db, order) == "ready_to_ship"

    async def test_a_failed_plate_is_reported_without_moving_the_card(
        self, db, fake_qbo, fake_bambuddy
    ):
        await seed_catalog(db)
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 77,
                "transactions": [{"transaction_id": 1, "sku": "PART-Y", "quantity": 2}],
            },
        )
        await db.commit()

        await printing.dispatch_pending(db)
        await db.commit()

        fake_bambuddy.queue[0]["status"] = "finished"
        fake_bambuddy.queue[1]["status"] = "failed"
        fake_bambuddy.queue[1]["error"] = "spaghetti detected"
        await printing.reconcile(db)
        await db.commit()

        await db.refresh(order)
        # The failure is on the line and the job; the card has not moved.
        assert order.status == "new"
        assert await suggests(db, order) == "in_production"

        failed = (
            (await db.execute(select(PrintJob).where(PrintJob.status == "failed")))
            .scalars()
            .all()
        )
        assert len(failed) == 1
        assert "spaghetti" in failed[0].error

        # Re-queueing sends it back out and the work completes.
        await printing.requeue_job(db, failed[0])
        await db.commit()
        for item in fake_bambuddy.queue:
            item["status"] = "finished"
        await printing.reconcile(db)
        await db.commit()

        await db.refresh(order)
        assert (await lines_by_sku(db, order.id))["PART-Y"].state == "ready"
        assert await suggests(db, order) == "ready_to_ship"
        assert order.status == "new"

    async def test_vanished_queued_job_is_not_assumed_successful(
        self, db, fake_qbo, fake_bambuddy
    ):
        await seed_catalog(db)
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 78,
                "transactions": [{"transaction_id": 1, "sku": "PART-Y", "quantity": 1}],
            },
        )
        await db.commit()
        await printing.dispatch_pending(db)
        await db.commit()

        # A queue read that comes back empty must not invent a completion.
        fake_bambuddy.queue = []
        await printing.reconcile(db)
        await db.commit()

        job = (await db.execute(select(PrintJob))).scalars().one()
        assert job.status == "queued"


class TestRelink:
    async def test_linking_a_product_reruns_intake_for_that_line(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order, _ = await intake.ingest_receipt(db, receipt())
        await db.commit()

        line = (await lines_by_sku(db, order.id))["MYSTERY-9"]
        assert line.state == "unmatched"

        await intake.relink_line(db, line, catalog["part_y"])
        await db.commit()

        await db.refresh(line)
        assert line.product_id == catalog["part_y"].id
        assert line.qty_to_print == 1
        jobs = (
            (await db.execute(select(PrintJob).where(PrintJob.order_line_id == line.id)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
