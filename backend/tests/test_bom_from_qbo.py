"""Building a bundle's BOM out of QuickBooks inventory.

The materials a bundle eats — filament, magnets, an insert somebody else makes —
are already in QuickBooks, and that is the copy the stock check reads. Making
the operator retype each one as a product and then link it back to the item they
picked it from is a step that exists only to be got wrong.

A component is still a product underneath, because that is what everything
downstream works in: allocation reads a product's QuickBooks item, printing
reads its mapping, the made-items sheet rolls up its BOM. So picking an item
finds the product that already points at it, or makes one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import BomLine, OrderLine, Product
from app.services import intake

pytestmark = pytest.mark.asyncio


class FakeQbo:
    async def get_items(self, item_ids):
        return {
            str(i): {"Id": str(i), "TrackQtyOnHand": True, "QtyOnHand": 20}
            for i in item_ids
        }


@pytest.fixture(autouse=True)
def stub_qbo(monkeypatch):
    async def client_for(_session):
        return FakeQbo()

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)


async def _bundle(db, sku="KIT"):
    product = Product(sku=sku, name=sku, fulfillment="bundle", qbo_item_id=None)
    db.add(product)
    await db.commit()
    return product


class TestAddingFromQuickBooks:
    async def test_an_item_with_no_product_gets_one(self, signed_in, db):
        bundle = await _bundle(db)
        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/bom/from-qbo",
                json={"qbo_item_id": "42", "qbo_item_name": "PLA Black 1kg", "quantity": 2},
            )
        ).json()

        assert body["created_product"] is True
        assert [(e["component_name"], e["quantity"]) for e in body["bom"]] == [
            ("PLA Black 1kg", 2)
        ]
        component = (
            await db.execute(select(Product).where(Product.qbo_item_id == "42"))
        ).scalar_one()
        # Stocked, because QuickBooks knowing its quantity is the whole point.
        assert component.fulfillment == "stocked"
        assert component.qbo_item_name == "PLA Black 1kg"

    async def test_an_item_that_already_has_a_product_reuses_it(self, signed_in, db):
        """Otherwise one material would end up as two products competing for it."""
        bundle = await _bundle(db)
        existing = Product(
            sku="PLA-BLACK", name="PLA, black", fulfillment="stocked", qbo_item_id="42"
        )
        db.add(existing)
        await db.commit()

        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/bom/from-qbo",
                json={"qbo_item_id": "42", "qbo_item_name": "PLA Black 1kg"},
            )
        ).json()
        assert body["created_product"] is False
        assert body["bom"][0]["component_sku"] == "PLA-BLACK"
        assert len((await db.execute(select(Product))).scalars().all()) == 2

    async def test_the_same_item_cannot_go_on_twice(self, signed_in, db):
        bundle = await _bundle(db)
        first = await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo", json={"qbo_item_id": "42"}
        )
        assert first.status_code == 201
        again = await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo", json={"qbo_item_id": "42"}
        )
        assert again.status_code == 409

    async def test_only_a_bundle_has_a_bom(self, signed_in, db):
        product = Product(sku="BIN", name="Bin", fulfillment="printed", qbo_item_id=None)
        db.add(product)
        await db.commit()
        response = await signed_in.post(
            f"/api/products/{product.id}/bom/from-qbo", json={"qbo_item_id": "42"}
        )
        assert response.status_code == 400

    async def test_a_bundle_cannot_eat_itself(self, signed_in, db):
        """It would have to be linked to the item first, but it can be."""
        bundle = await _bundle(db)
        bundle.qbo_item_id = "42"
        await db.commit()
        response = await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo", json={"qbo_item_id": "42"}
        )
        assert response.status_code == 400
        assert "itself" in response.json()["detail"]

    async def test_an_unnamed_item_still_gets_a_usable_product(self, signed_in, db):
        bundle = await _bundle(db)
        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/bom/from-qbo", json={"qbo_item_id": "42"}
            )
        ).json()
        assert body["bom"][0]["component_name"] == "QuickBooks item 42"


class TestItFlowsThroughAnOrder:
    async def test_the_material_is_drawn_from_its_quickbooks_stock(self, signed_in, db):
        """The reason for doing it this way: the stock check just works."""
        bundle = await _bundle(db)
        await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo",
            json={"qbo_item_id": "42", "qbo_item_name": "PLA Black 1kg", "quantity": 3},
        )

        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 1,
                "name": "Ada",
                "transactions": [{"transaction_id": 1, "sku": "KIT", "quantity": 2}],
            },
        )
        await db.commit()

        child = (
            await db.execute(
                select(OrderLine).where(OrderLine.parent_line_id.isnot(None))
            )
        ).scalar_one()
        assert child.quantity == 6
        # QuickBooks says 20 on hand, so all six come from stock and none print.
        assert (child.qty_from_stock, child.qty_to_print) == (6, 0)
        assert child.stock_note is None

    async def test_the_line_is_the_component_the_bom_names(self, signed_in, db):
        bundle = await _bundle(db)
        await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo",
            json={"qbo_item_id": "42", "qbo_item_name": "PLA Black 1kg"},
        )
        entry = (await db.execute(select(BomLine))).scalar_one()
        component = await db.get(Product, entry.component_id)
        assert component.qbo_item_id == "42"
