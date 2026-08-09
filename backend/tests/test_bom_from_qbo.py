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


# --------------------------------------------------------------------------
# A variation of a bundle, needing something the BOM does not have
# --------------------------------------------------------------------------


class TestOptionRulesFromQuickBooks:
    """A variation exists because it needs something the base build does not.

    The fan, the bigger magnet, the second colour — by definition not on the
    BOM, and often not a product yet either. Offering only what is already on
    the BOM, or only what is already a product, is offering the wrong list.
    """

    async def test_a_rule_can_bring_in_an_item_that_is_not_a_product(
        self, signed_in, db
    ):
        bundle = await _bundle(db)
        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/option-rules/from-qbo",
                json={
                    "option_name": "Bin Fan",
                    "option_value": "Yes",
                    "qbo_item_id": "9",
                    "qbo_item_name": "40mm fan",
                    "quantity": 1,
                },
            )
        ).json()

        assert body["created_product"] is True
        rule = body["option_rules"][0]
        assert (rule["option_name"], rule["option_value"]) == ("Bin Fan", "Yes")
        component = (
            await db.execute(select(Product).where(Product.qbo_item_id == "9"))
        ).scalar_one()
        assert component.name == "40mm fan"
        assert component.fulfillment == "stocked"

    async def test_it_reuses_a_product_that_already_has_the_item(self, signed_in, db):
        bundle = await _bundle(db)
        existing = Product(
            sku="FAN-40", name="Fan, 40mm", fulfillment="stocked", qbo_item_id="9"
        )
        db.add(existing)
        await db.commit()

        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/option-rules/from-qbo",
                json={"option_name": "Bin Fan", "option_value": "Yes", "qbo_item_id": "9"},
            )
        ).json()
        assert body["created_product"] is False
        assert body["option_rules"][0]["component_sku"] == "FAN-40"

    async def test_it_can_swap_something_already_on_the_bom(self, signed_in, db):
        bundle = await _bundle(db)
        await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo",
            json={"qbo_item_id": "1", "qbo_item_name": "PLA Black 1kg"},
        )
        grey = (
            await db.execute(select(Product).where(Product.qbo_item_id == "1"))
        ).scalar_one()

        body = (
            await signed_in.post(
                f"/api/products/{bundle.id}/option-rules/from-qbo",
                json={
                    "option_name": "Colour",
                    "option_value": "Red",
                    "replaces_id": str(grey.id),
                    "qbo_item_id": "2",
                    "qbo_item_name": "PLA Red 1kg",
                },
            )
        ).json()
        rule = body["option_rules"][0]
        assert rule["replaces_sku"] == grey.sku
        assert rule["component_sku"] != grey.sku

    async def test_it_will_not_swap_something_the_bundle_does_not_have(
        self, signed_in, db
    ):
        """The rule would be skipped at intake and the order built wrong."""
        bundle = await _bundle(db)
        stranger = Product(
            sku="STRANGER", name="Stranger", fulfillment="stocked", qbo_item_id=None
        )
        db.add(stranger)
        await db.commit()

        response = await signed_in.post(
            f"/api/products/{bundle.id}/option-rules/from-qbo",
            json={
                "option_name": "Colour",
                "option_value": "Red",
                "replaces_id": str(stranger.id),
                "qbo_item_id": "2",
            },
        )
        assert response.status_code == 400
        assert "not on this bundle's BOM" in response.json()["detail"]

    async def test_only_a_bundle_has_options_that_change_a_bom(self, signed_in, db):
        product = Product(sku="BIN", name="Bin", fulfillment="printed", qbo_item_id=None)
        db.add(product)
        await db.commit()
        response = await signed_in.post(
            f"/api/products/{product.id}/option-rules/from-qbo",
            json={"option_name": "Colour", "option_value": "Red", "qbo_item_id": "2"},
        )
        assert response.status_code == 400

    async def test_the_same_rule_cannot_be_added_twice(self, signed_in, db):
        bundle = await _bundle(db)
        payload = {"option_name": "Bin Fan", "option_value": "Yes", "qbo_item_id": "9"}
        assert (
            await signed_in.post(
                f"/api/products/{bundle.id}/option-rules/from-qbo", json=payload
            )
        ).status_code == 201
        again = await signed_in.post(
            f"/api/products/{bundle.id}/option-rules/from-qbo", json=payload
        )
        assert again.status_code == 409


class TestTheVariationGetsItsMaterial:
    async def test_an_order_for_that_variation_pulls_the_extra_item(
        self, signed_in, db
    ):
        """End to end: the buyer picks the option, the fan comes off the shelf."""
        bundle = await _bundle(db)
        await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo",
            json={"qbo_item_id": "1", "qbo_item_name": "PLA Black 1kg", "quantity": 1},
        )
        await signed_in.post(
            f"/api/products/{bundle.id}/option-rules/from-qbo",
            json={
                "option_name": "Bin Fan",
                "option_value": "Yes",
                "qbo_item_id": "9",
                "qbo_item_name": "40mm fan",
                "quantity": 2,
            },
        )

        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 5,
                "name": "Ada",
                "transactions": [
                    {
                        "transaction_id": 5,
                        "sku": "KIT",
                        "quantity": 1,
                        "variations": [
                            {"formatted_name": "Bin Fan", "formatted_value": "Yes"}
                        ],
                    }
                ],
            },
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
        by_name = {
            (await db.get(Product, child.product_id)).name: child for child in children
        }
        assert set(by_name) == {"PLA Black 1kg", "40mm fan"}
        assert by_name["40mm fan"].quantity == 2
        # Drawn from the stock QuickBooks reports for that item.
        assert by_name["40mm fan"].qty_from_stock == 2

    async def test_the_other_variation_does_not_get_it(self, signed_in, db):
        bundle = await _bundle(db)
        await signed_in.post(
            f"/api/products/{bundle.id}/bom/from-qbo",
            json={"qbo_item_id": "1", "qbo_item_name": "PLA Black 1kg"},
        )
        await signed_in.post(
            f"/api/products/{bundle.id}/option-rules/from-qbo",
            json={
                "option_name": "Bin Fan",
                "option_value": "Yes",
                "qbo_item_id": "9",
                "qbo_item_name": "40mm fan",
            },
        )

        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 6,
                "name": "Ada",
                "transactions": [
                    {
                        "transaction_id": 6,
                        "sku": "KIT",
                        "quantity": 1,
                        "variations": [
                            {"formatted_name": "Bin Fan", "formatted_value": "No"}
                        ],
                    }
                ],
            },
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
        names = {(await db.get(Product, c.product_id)).name for c in children}
        assert names == {"PLA Black 1kg"}
