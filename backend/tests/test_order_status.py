"""An order's status, which only a person ever sets.

Nothing moves a card: not intake, not a finished plate, not buying a label. The
rules can see that four plates came off the printers; they cannot see that the
parcel is still on the bench or that the buyer rang up. A card that moves itself
out from under whoever is working the board is worse than one that waits.

So the arrow points from the status to the lines. Cancelling an order cancels
them, which is what releases their stock and keeps their plates off the
printers; Shipped carries them to shipped. Moving the card back recomputes all
of it, because none of it is written down separately.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import BomLine, OrderLine, PrintJob, PrintMapping, Product
from app.services import allocation, intake, printing
from app.services.state import recompute_order

pytestmark = pytest.mark.asyncio


class FakeQbo:
    def __init__(self, on_hand=10):
        self.on_hand = on_hand

    async def get_items(self, item_ids):
        return {
            str(i): {"Id": str(i), "TrackQtyOnHand": True, "QtyOnHand": self.on_hand}
            for i in item_ids
        }


@pytest.fixture(autouse=True)
def stub_qbo(monkeypatch):
    async def client_for(_session):
        return FakeQbo()

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)


def _receipt(receipt_id, *, sku="BIN", quantity=1):
    return {
        "receipt_id": receipt_id,
        "name": "Ada",
        "created_timestamp": 1_780_000_000,
        "transactions": [
            {
                "transaction_id": receipt_id * 10,
                "sku": sku,
                "title": "Storage bin",
                "quantity": quantity,
            }
        ],
    }


async def _printed_product(db, sku="BIN"):
    product = Product(sku=sku, name=sku, fulfillment="printed", qbo_item_id=None)
    db.add(product)
    await db.flush()
    db.add(
        PrintMapping(
            product_id=product.id, bambuddy_archive_id=10, plate_number=1, units_per_plate=1
        )
    )
    await db.commit()
    return product


# --------------------------------------------------------------------------
# Setting and clearing
# --------------------------------------------------------------------------


class TestSettingStatus:
    async def test_an_order_can_be_moved_to_a_column(self, signed_in, db):
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(1))
        await db.commit()
        assert order.status == "new"

        body = (
            await signed_in.put(
                f"/api/orders/{order.id}/status",
                json={"status": "ready_to_ship", "note": "Collected in person"},
            )
        ).json()
        assert body["status"] == "ready_to_ship"
        assert body["status_note"] == "Collected in person"

    async def test_an_order_arrives_in_new_and_stays_there(self, signed_in, db):
        """Intake matches, allocates and plans plates — and moves nothing."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(6))
        await db.commit()
        assert order.status == "new"

        await intake.process_order(db, order)
        await recompute_order(db, order)
        await db.commit()
        await db.refresh(order)
        assert order.status == "new"

    async def test_the_rules_never_take_it_back(self, signed_in, db):
        """Anything at all recomputes the order; the choice has to survive that."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(2))
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "ready_to_ship"}
        )

        await intake.process_order(db, order)
        await recompute_order(db, order)
        await db.commit()
        await db.refresh(order)
        assert order.status == "ready_to_ship"

    async def test_the_rules_suggest_without_acting(self, signed_in, db):
        """They still have an opinion; they just do not get to apply it."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(3))
        await db.commit()

        # Move it somewhere the work plainly is not.
        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "ready_to_ship"}
        )
        body = (await signed_in.get(f"/api/orders/{order.id}")).json()
        assert body["status"] == "ready_to_ship"
        assert body["suggested_status"] == "new"

    async def test_the_suggestion_follows_the_work(self, signed_in, db, monkeypatch):
        """Once the plates are actually out, the rules say In Production."""

        class Bambuddy:
            async def enqueue(self, **kwargs):
                return {"id": 1}

        async def client_for(_session):
            return Bambuddy()

        monkeypatch.setattr(printing.bambuddy_api, "client_for", client_for)

        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(7))
        await db.commit()
        assert (await signed_in.get(f"/api/orders/{order.id}")).json()[
            "suggested_status"
        ] == "new"

        await printing.dispatch_pending(db)
        await db.commit()
        body = (await signed_in.get(f"/api/orders/{order.id}")).json()
        assert body["suggested_status"] == "in_production"
        assert body["status"] == "new"

    async def test_a_status_that_is_not_a_column_is_refused(self, signed_in, db):
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(4))
        await db.commit()
        response = await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "somewhere_else"}
        )
        assert response.status_code == 400

    async def test_the_change_is_written_to_the_audit_log(self, signed_in, db):
        from app.models import AuditLog

        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(5))
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/status",
            json={"status": "cancelled", "note": "Buyer rang up"},
        )
        entry = (
            await db.execute(select(AuditLog).where(AuditLog.action == "set_status"))
        ).scalar_one()
        assert entry.detail["to"] == "cancelled"
        assert entry.detail["note"] == "Buyer rang up"


# --------------------------------------------------------------------------
# What the status actually does
# --------------------------------------------------------------------------


class TestCancelling:
    async def test_it_carries_the_lines_with_it(self, signed_in, db):
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(10))
        await db.commit()

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )
        states = [
            line.state for line in (await db.execute(select(OrderLine))).scalars().all()
        ]
        assert states == ["cancelled"]

    async def test_it_releases_the_stock_the_order_was_holding(self, signed_in, db):
        """A cancelled order that still reserves stock starves the live ones."""
        product = Product(sku="BIN", name="Bin", fulfillment="stocked", qbo_item_id="7")
        db.add(product)
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(11, quantity=3))
        await db.commit()
        assert await allocation.reserved_quantities(db) == {"7": 3}

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )
        assert await allocation.reserved_quantities(db) == {}

    async def test_its_plates_are_not_sent_to_bambuddy(self, signed_in, db, monkeypatch):
        """Cancelling did not delete the plates it had already planned."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(12))
        await db.commit()
        assert len((await db.execute(select(PrintJob))).scalars().all()) == 1

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )

        sent: list[dict] = []

        class Bambuddy:
            async def enqueue(self, **kwargs):
                sent.append(kwargs)
                return {"id": 1}

        async def client_for(_session):
            return Bambuddy()

        monkeypatch.setattr(printing.bambuddy_api, "client_for", client_for)
        assert await printing.dispatch_pending(db) == {"dispatched": 0, "failed": 0}
        assert sent == []

    async def test_a_line_cancelled_on_its_own_is_not_printed_either(
        self, signed_in, db, monkeypatch
    ):
        """Same failure, and it was there before order-level cancelling existed."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(13))
        await db.commit()
        line = (await db.execute(select(OrderLine))).scalar_one()

        await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/override",
            json={"action": "cancel"},
        )

        sent: list[dict] = []

        class Bambuddy:
            async def enqueue(self, **kwargs):
                sent.append(kwargs)
                return {"id": 1}

        async def client_for(_session):
            return Bambuddy()

        monkeypatch.setattr(printing.bambuddy_api, "client_for", client_for)
        await printing.dispatch_pending(db)
        assert sent == []

    async def test_moving_it_back_puts_everything_back(self, signed_in, db):
        product = Product(sku="BIN", name="Bin", fulfillment="stocked", qbo_item_id="7")
        db.add(product)
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(14, quantity=2))
        await db.commit()

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )
        assert await allocation.reserved_quantities(db) == {}

        await signed_in.put(f"/api/orders/{order.id}/status", json={"status": "new"})
        assert await allocation.reserved_quantities(db) == {"7": 2}
        line = (await db.execute(select(OrderLine))).scalar_one()
        assert line.state != "cancelled"

    async def test_a_bundle_and_its_components_all_go(self, signed_in, db):
        bundle = Product(sku="BIN", name="Bin", fulfillment="bundle", qbo_item_id=None)
        part = Product(sku="PART", name="Part", fulfillment="stocked", qbo_item_id=None)
        db.add_all([bundle, part])
        await db.flush()
        db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=2))
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(15))
        await db.commit()

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )
        states = {
            line.state for line in (await db.execute(select(OrderLine))).scalars().all()
        }
        assert states == {"cancelled"}


class TestShipping:
    async def test_marking_it_shipped_carries_the_lines(self, signed_in, db):
        """For an order that went out without a ShipStation label."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(20))
        await db.commit()

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "shipped"}
        )
        states = [
            line.state for line in (await db.execute(select(OrderLine))).scalars().all()
        ]
        assert states == ["shipped"]

    async def test_a_shipped_order_stops_reserving_stock(self, signed_in, db):
        product = Product(sku="BIN", name="Bin", fulfillment="stocked", qbo_item_id="7")
        db.add(product)
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(21, quantity=4))
        await db.commit()
        assert await allocation.reserved_quantities(db) == {"7": 4}

        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "shipped"}
        )
        assert await allocation.reserved_quantities(db) == {}


# --------------------------------------------------------------------------
# Where it shows up
# --------------------------------------------------------------------------


class TestVisibility:
    async def test_the_board_puts_it_in_the_chosen_column(self, signed_in, db):
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(30))
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "assembly"}
        )

        board = (await signed_in.get("/api/board")).json()
        column = next(c for c in board["columns"] if c["key"] == "assembly")
        assert [o["order_number"] for o in column["orders"]] == ["30"]

    async def test_cancelled_is_a_column_of_its_own(self, signed_in, db):
        """Draggable to any status means every status has somewhere to drop."""
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(31))
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "cancelled"}
        )

        board = (await signed_in.get("/api/board")).json()
        cancelled = next(c for c in board["columns"] if c["key"] == "cancelled")
        assert [o["order_number"] for o in cancelled["orders"]] == ["31"]
        assert all(
            not column["orders"] for column in board["columns"]
            if column["key"] != "cancelled"
        )

    async def test_a_status_the_board_does_not_know_is_refused(self, signed_in, db):
        await _printed_product(db)
        order, _ = await intake.ingest_receipt(db, _receipt(32))
        await db.commit()
        response = await signed_in.put(
            f"/api/orders/{order.id}/status", json={"status": "somewhere"}
        )
        assert response.status_code == 400
