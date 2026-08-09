"""Etsy options, and what they do to a bill of materials.

A buyer picking "Color: Red" changes what comes off the shelf. Getting this
wrong is quiet: the order looks entirely normal on the board and the wrong
filament goes on the printer, which nobody discovers until the parcel is open.
So these tests care as much about the warnings as about the arithmetic.
"""

from __future__ import annotations

import pytest

from app.models import BomLine, BomOptionRule, Order, OrderLine, Product
from app.services import bom_options, intake

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Reading the options off a transaction
# --------------------------------------------------------------------------


class TestExtractingOptions:
    async def test_the_documented_shape_is_read(self):
        found = intake.extract_variations(
            {
                "variations": [
                    {
                        "property_id": 200,
                        "value_id": 1213,
                        "formatted_name": "Color",
                        "formatted_value": "Red",
                    }
                ]
            }
        )
        assert found == [
            {"name": "Color", "value": "Red", "property_id": 200, "value_id": 1213}
        ]

    async def test_plainer_spellings_are_accepted(self):
        """The field names have moved across Etsy API versions and shapes."""
        found = intake.extract_variations(
            {"variations": [{"name": "Size", "value": "Large"}]}
        )
        assert found == [{"name": "Size", "value": "Large"}]

    async def test_a_half_filled_variation_is_skipped(self):
        found = intake.extract_variations(
            {"variations": [{"formatted_name": "Color"}, {"formatted_value": "Red"}]}
        )
        assert found == []

    async def test_personalisation_is_kept_but_marked_free_text(self):
        """It is for a human to read; matching a rule on it would fire on luck."""
        found = intake.extract_variations({"personalization": "  For Ada  "})
        assert found == [
            {"name": "Personalization", "value": "For Ada", "free_text": True}
        ]

    async def test_nothing_chosen_is_an_empty_list(self):
        assert intake.extract_variations({}) == []
        assert intake.extract_variations({"variations": None}) == []

    async def test_whitespace_is_trimmed(self):
        found = intake.extract_variations(
            {"variations": [{"formatted_name": " Color ", "formatted_value": " Red "}]}
        )
        assert found[0] == {"name": "Color", "value": "Red"}


# --------------------------------------------------------------------------
# Applying options to a BOM — pure, no database
# --------------------------------------------------------------------------


class _FakeProduct:
    def __init__(self, sku, id=None):
        self.id = id or sku
        self.sku = sku
        self.name = sku


class _FakeBomLine:
    def __init__(self, component, quantity):
        self.component = component
        self.component_id = component.id
        self.quantity = quantity


class _FakeRule:
    def __init__(self, name, value, component, replaces=None, quantity=None):
        self.option_name = name
        self.option_value = value
        self.component = component
        self.replaces = replaces
        self.replaces_id = replaces.id if replaces else None
        self.quantity = quantity


def _picked(**options):
    return [{"name": name, "value": value} for name, value in options.items()]


class TestApplyingOptions:
    def setup_method(self):
        self.grey = _FakeProduct("PLA-GREY")
        self.red = _FakeProduct("PLA-RED")
        self.blue = _FakeProduct("PLA-BLUE")
        self.body = _FakeProduct("EGG-BODY")
        self.box = _FakeProduct("GIFT-BOX")
        self.bom = [_FakeBomLine(self.body, 1), _FakeBomLine(self.grey, 2)]

    async def test_no_rules_leaves_the_bom_alone(self):
        result = bom_options.resolve(self.bom, [], _picked(Color="Red"))
        assert [(p.sku, q) for p, q in result.components] == [("EGG-BODY", 1), ("PLA-GREY", 2)]
        assert result.warnings == []

    async def test_a_matching_rule_swaps_the_component(self):
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.grey)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Red"))
        assert [(p.sku, q) for p, q in result.components] == [("EGG-BODY", 1), ("PLA-RED", 2)]
        # The swapped-in component inherits the quantity it replaced.
        assert "PLA-GREY → PLA-RED" in result.applied[0]

    async def test_a_non_matching_rule_does_nothing(self):
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.grey)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Blue"))
        assert [p.sku for p, _ in result.components] == ["EGG-BODY", "PLA-GREY"]

    async def test_matching_ignores_case_and_padding(self):
        """These strings are typed by hand in Etsy's listing editor."""
        rules = [_FakeRule(" color ", "RED", self.red, replaces=self.grey)]
        result = bom_options.resolve(
            self.bom, rules, [{"name": "Color", "value": " red "}]
        )
        assert [p.sku for p, _ in result.components] == ["EGG-BODY", "PLA-RED"]

    async def test_a_rule_can_add_rather_than_swap(self):
        rules = [_FakeRule("Gift box", "Yes", self.box, quantity=1)]
        result = bom_options.resolve(self.bom, rules, _picked(**{"Gift box": "Yes"}))
        assert ("GIFT-BOX", 1) in [(p.sku, q) for p, q in result.components]
        assert "added GIFT-BOX" in result.applied[0]

    async def test_a_swap_can_override_the_quantity(self):
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.grey, quantity=5)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Red"))
        assert ("PLA-RED", 5) in [(p.sku, q) for p, q in result.components]

    async def test_two_options_both_apply(self):
        rules = [
            _FakeRule("Color", "Red", self.red, replaces=self.grey),
            _FakeRule("Gift box", "Yes", self.box, quantity=1),
        ]
        result = bom_options.resolve(
            self.bom, rules, _picked(Color="Red", **{"Gift box": "Yes"})
        )
        assert sorted(p.sku for p, _ in result.components) == [
            "EGG-BODY",
            "GIFT-BOX",
            "PLA-RED",
        ]

    async def test_adding_a_component_already_present_sums_it(self):
        rules = [_FakeRule("Extra", "Yes", self.grey, quantity=3)]
        result = bom_options.resolve(self.bom, rules, _picked(Extra="Yes"))
        assert ("PLA-GREY", 5) in [(p.sku, q) for p, q in result.components]

    async def test_the_bom_order_is_kept(self):
        """A component's place on the board should not jump because a rule fired."""
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.body)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Red"))
        assert [p.sku for p, _ in result.components] == ["PLA-GREY", "PLA-RED"]

    async def test_free_text_never_matches_a_rule(self):
        rules = [_FakeRule("Personalization", "For Ada", self.red, replaces=self.grey)]
        result = bom_options.resolve(
            self.bom,
            rules,
            [{"name": "Personalization", "value": "For Ada", "free_text": True}],
        )
        assert [p.sku for p, _ in result.components] == ["EGG-BODY", "PLA-GREY"]


class TestWarningsAboutOptions:
    def setup_method(self):
        self.grey = _FakeProduct("PLA-GREY")
        self.red = _FakeProduct("PLA-RED")
        self.missing = _FakeProduct("NOT-ON-BOM")
        self.bom = [_FakeBomLine(self.grey, 2)]

    async def test_an_uncovered_value_is_flagged(self):
        """Silence here is the dangerous case: it builds, and it is wrong."""
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.grey)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Teal"))
        assert len(result.warnings) == 1
        assert "No rule for Color = Teal" in result.warnings[0]
        # And the base BOM was used, not nothing.
        assert [p.sku for p, _ in result.components] == ["PLA-GREY"]

    async def test_an_option_with_no_rules_at_all_is_not_flagged(self):
        """Most options on a listing have nothing to do with the BOM."""
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.grey)]
        result = bom_options.resolve(
            self.bom, rules, _picked(Color="Red", Wrapping="Kraft")
        )
        assert result.warnings == []

    async def test_a_stale_rule_is_flagged_and_skipped(self):
        """Swapping out something the BOM no longer has would inflate the build."""
        rules = [_FakeRule("Color", "Red", self.red, replaces=self.missing)]
        result = bom_options.resolve(self.bom, rules, _picked(Color="Red"))
        assert "not in this BOM" in result.warnings[0]
        assert [p.sku for p, _ in result.components] == ["PLA-GREY"]


# --------------------------------------------------------------------------
# End to end through intake
# --------------------------------------------------------------------------


async def _product(session, sku, fulfillment="stocked"):
    product = Product(sku=sku, name=sku, fulfillment=fulfillment, qbo_item_id=None)
    session.add(product)
    await session.flush()
    await session.refresh(product)
    return product


def _receipt(receipt_id, sku, variations, quantity=1):
    return {
        "receipt_id": receipt_id,
        "name": "Ada",
        "created_timestamp": 1_780_000_000,
        "transactions": [
            {
                "transaction_id": receipt_id * 10,
                "listing_id": 999,
                "sku": sku,
                "title": sku,
                "quantity": quantity,
                "variations": variations,
            }
        ],
    }


class TestIntakeUsesTheChosenOptions:
    async def _shop(self, db):
        grey = await _product(db, "PLA-GREY")
        red = await _product(db, "PLA-RED")
        body = await _product(db, "EGG-BODY", fulfillment="printed")
        egg = await _product(db, "EGG-DRAGON", fulfillment="bundle")
        db.add(BomLine(bundle_id=egg.id, component_id=body.id, quantity=1))
        db.add(BomLine(bundle_id=egg.id, component_id=grey.id, quantity=2))
        await db.flush()
        return {"grey": grey, "red": red, "body": body, "egg": egg}

    async def _parent(self, db, order):
        return (await db.execute(
            __import__("sqlalchemy").select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )).scalar_one()

    async def _components(self, db, order):
        lines = (await db.execute(
            __import__("sqlalchemy").select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.isnot(None)
            )
        )).scalars().all()
        out = {}
        for line in lines:
            product = await db.get(Product, line.product_id)
            out[product.sku] = line.quantity
        return out

    async def test_a_colour_choice_swaps_the_filament(self, db):
        shop = await self._shop(db)
        db.add(
            BomOptionRule(
                bundle_id=shop["egg"].id,
                option_name="Color",
                option_value="Red",
                replaces_id=shop["grey"].id,
                component_id=shop["red"].id,
            )
        )
        await db.flush()

        order, created = await intake.ingest_receipt(
            db,
            _receipt(1, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]),
        )
        assert created
        assert await self._components(db, order) == {"EGG-BODY": 1, "PLA-RED": 2}

    async def test_quantities_multiply_through_the_swap(self, db):
        shop = await self._shop(db)
        db.add(
            BomOptionRule(
                bundle_id=shop["egg"].id,
                option_name="Color",
                option_value="Red",
                replaces_id=shop["grey"].id,
                component_id=shop["red"].id,
            )
        )
        await db.flush()

        order, _ = await intake.ingest_receipt(
            db,
            _receipt(
                2,
                "EGG-DRAGON",
                [{"formatted_name": "Color", "formatted_value": "Red"}],
                quantity=3,
            ),
        )
        assert await self._components(db, order) == {"EGG-BODY": 3, "PLA-RED": 6}

    async def test_without_a_matching_rule_the_base_bom_is_used_and_flagged(self, db):
        shop = await self._shop(db)
        db.add(
            BomOptionRule(
                bundle_id=shop["egg"].id,
                option_name="Color",
                option_value="Red",
                replaces_id=shop["grey"].id,
                component_id=shop["red"].id,
            )
        )
        await db.flush()

        order, _ = await intake.ingest_receipt(
            db,
            _receipt(3, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Teal"}]),
        )
        assert await self._components(db, order) == {"EGG-BODY": 1, "PLA-GREY": 2}

        parent = (await db.execute(
            __import__("sqlalchemy").select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )).scalar_one()
        assert "No rule for Color = Teal" in (parent.stock_note or "")

    async def test_the_options_are_stored_on_the_line(self, db):
        await self._shop(db)
        order, _ = await intake.ingest_receipt(
            db,
            _receipt(4, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]),
        )
        parent = (await db.execute(
            __import__("sqlalchemy").select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )).scalar_one()
        assert parent.variations == [{"name": "Color", "value": "Red"}]

    async def test_changing_the_rule_and_reprocessing_moves_the_component(self, db):
        """Nothing printed yet, so the stale component line should not linger."""
        shop = await self._shop(db)
        order, _ = await intake.ingest_receipt(
            db,
            _receipt(5, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]),
        )
        assert await self._components(db, order) == {"EGG-BODY": 1, "PLA-GREY": 2}

        db.add(
            BomOptionRule(
                bundle_id=shop["egg"].id,
                option_name="Color",
                option_value="Red",
                replaces_id=shop["grey"].id,
                component_id=shop["red"].id,
            )
        )
        await db.flush()
        await intake.process_order(db, order)
        assert await self._components(db, order) == {"EGG-BODY": 1, "PLA-RED": 2}

    async def test_an_order_already_on_the_board_gains_its_options_on_the_next_poll(
        self, db
    ):
        """The branch every existing order takes on every poll.

        An order ingested before options were captured returns early from
        ingest_receipt, so nothing ever filled them in and the board stayed
        blank — which is exactly what happened in the field.
        """
        shop = await self._shop(db)
        receipt = _receipt(
            7, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]
        )

        # Ingest as it would have been before options existed.
        stripped = {**receipt, "transactions": [
            {k: v for k, v in receipt["transactions"][0].items() if k != "variations"}
        ]}
        order, created = await intake.ingest_receipt(db, stripped)
        assert created
        parent = await self._parent(db, order)
        assert parent.variations == []

        db.add(
            BomOptionRule(
                bundle_id=shop["egg"].id,
                option_name="Color",
                option_value="Red",
                replaces_id=shop["grey"].id,
                component_id=shop["red"].id,
            )
        )
        await db.flush()

        # The poller sees the same receipt again, this time carrying options.
        order, created = await intake.ingest_receipt(db, receipt)
        assert not created
        parent = await self._parent(db, order)
        assert parent.variations == [{"name": "Color", "value": "Red"}]
        # And the BOM was re-resolved, because an option can change it.
        assert await self._components(db, order) == {"EGG-BODY": 1, "PLA-RED": 2}

    async def test_a_repeat_poll_with_nothing_new_does_not_reprocess(self, db, monkeypatch):
        """Every open order hits this branch on every poll; it must stay cheap."""
        await self._shop(db)
        receipt = _receipt(
            8, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]
        )
        await intake.ingest_receipt(db, receipt)

        calls = {"n": 0}
        original = intake.process_order

        async def counted(session, order):
            calls["n"] += 1
            return await original(session, order)

        monkeypatch.setattr(intake, "process_order", counted)
        await intake.ingest_receipt(db, receipt)
        assert calls["n"] == 0

    async def test_options_are_backfilled_from_the_stored_receipt(self, db):
        """Orders taken before options were captured still have them in the payload."""
        await self._shop(db)
        receipt = _receipt(
            6, "EGG-DRAGON", [{"formatted_name": "Color", "formatted_value": "Red"}]
        )
        order = Order(
            etsy_receipt_id=6, order_number="6", buyer_name="Ada", raw=receipt
        )
        db.add(order)
        await db.flush()
        db.add(
            OrderLine(
                order_id=order.id,
                sku_raw="EGG-DRAGON",
                quantity=1,
                etsy_transaction_id=60,
                variations=[],
            )
        )
        await db.flush()

        filled = await intake.backfill_variations(db, order)
        assert filled == 1
        line = (await db.execute(
            __import__("sqlalchemy").select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )).scalar_one()
        assert line.variations == [{"name": "Color", "value": "Red"}]


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


class TestTheOptionRulesApi:
    async def _bundle(self, signed_in):
        grey = (await signed_in.post(
            "/api/products", json={"sku": "PLA-GREY", "name": "Grey", "fulfillment": "stocked"}
        )).json()
        red = (await signed_in.post(
            "/api/products", json={"sku": "PLA-RED", "name": "Red", "fulfillment": "stocked"}
        )).json()
        egg = (await signed_in.post(
            "/api/products", json={"sku": "EGG", "name": "Egg", "fulfillment": "bundle"}
        )).json()
        await signed_in.post(
            f"/api/products/{egg['id']}/bom",
            json={"component_id": grey["id"], "quantity": 2},
        )
        return grey, red, egg

    async def test_a_swap_rule_round_trips(self, signed_in, db):
        grey, red, egg = await self._bundle(signed_in)
        response = await signed_in.post(
            f"/api/products/{egg['id']}/option-rules",
            json={
                "option_name": "Color",
                "option_value": "Red",
                "component_id": red["id"],
                "replaces_id": grey["id"],
            },
        )
        assert response.status_code == 201
        rule = response.json()["option_rules"][0]
        assert rule["option_name"] == "Color"
        assert rule["replaces_sku"] == "PLA-GREY"
        assert rule["component_sku"] == "PLA-RED"

    async def test_replacing_something_not_on_the_bom_is_refused(self, signed_in, db):
        """The rule would be skipped at intake and the order built wrong."""
        _, red, egg = await self._bundle(signed_in)
        other = (await signed_in.post(
            "/api/products", json={"sku": "OTHER", "name": "Other", "fulfillment": "stocked"}
        )).json()
        response = await signed_in.post(
            f"/api/products/{egg['id']}/option-rules",
            json={
                "option_name": "Color",
                "option_value": "Red",
                "component_id": red["id"],
                "replaces_id": other["id"],
            },
        )
        assert response.status_code == 400
        assert "not on this bundle's BOM" in response.json()["detail"]

    async def test_only_a_bundle_can_have_rules(self, signed_in, db):
        grey, red, _ = await self._bundle(signed_in)
        response = await signed_in.post(
            f"/api/products/{grey['id']}/option-rules",
            json={"option_name": "Color", "option_value": "Red", "component_id": red["id"]},
        )
        assert response.status_code == 400
        assert "Only a bundle" in response.json()["detail"]

    async def test_the_same_rule_twice_is_refused(self, signed_in, db):
        grey, red, egg = await self._bundle(signed_in)
        payload = {
            "option_name": "Color",
            "option_value": "Red",
            "component_id": red["id"],
            "replaces_id": grey["id"],
        }
        assert (
            await signed_in.post(f"/api/products/{egg['id']}/option-rules", json=payload)
        ).status_code == 201
        second = await signed_in.post(
            f"/api/products/{egg['id']}/option-rules", json=payload
        )
        assert second.status_code == 409

    async def test_case_only_differences_are_the_same_rule(self, signed_in, db):
        grey, red, egg = await self._bundle(signed_in)
        await signed_in.post(
            f"/api/products/{egg['id']}/option-rules",
            json={
                "option_name": "Color",
                "option_value": "Red",
                "component_id": red["id"],
                "replaces_id": grey["id"],
            },
        )
        clash = await signed_in.post(
            f"/api/products/{egg['id']}/option-rules",
            json={
                "option_name": "color",
                "option_value": "RED",
                "component_id": red["id"],
                "replaces_id": grey["id"],
            },
        )
        assert clash.status_code == 409

    async def test_a_rule_can_be_removed(self, signed_in, db):
        grey, red, egg = await self._bundle(signed_in)
        created = (await signed_in.post(
            f"/api/products/{egg['id']}/option-rules",
            json={
                "option_name": "Color",
                "option_value": "Red",
                "component_id": red["id"],
                "replaces_id": grey["id"],
            },
        )).json()
        rule_id = created["option_rules"][0]["id"]
        after = await signed_in.delete(
            f"/api/products/{egg['id']}/option-rules/{rule_id}"
        )
        assert after.status_code == 200
        assert after.json()["option_rules"] == []


class TestOptionsSeenOnRealOrders:
    """Rules match Etsy's own strings, which live nowhere else in PrintFlow.
    Asking someone to retype them from memory is how a rule silently never
    fires, so the choices come from what actually arrived."""

    async def test_it_reports_names_and_values_with_counts(self, signed_in, db):
        egg = await _product(db, "EGG-DRAGON", fulfillment="printed")
        await db.commit()

        for index, colour in enumerate(["Red", "Red", "Blue"]):
            await intake.ingest_receipt(
                db,
                _receipt(
                    100 + index,
                    "EGG-DRAGON",
                    [{"formatted_name": "Color", "formatted_value": colour}],
                ),
            )
        await db.commit()

        body = (
            await signed_in.get(f"/api/products/{egg.id}/observed-options")
        ).json()
        assert [o["name"] for o in body["options"]] == ["Color"]
        assert body["options"][0]["values"] == [
            {"value": "Red", "orders": 2},
            {"value": "Blue", "orders": 1},
        ]

    async def test_free_text_is_not_offered_as_a_rule_value(self, signed_in, db):
        """Nobody wants a rule for one buyer's engraving message."""
        egg = await _product(db, "EGG-DRAGON", fulfillment="printed")
        await db.commit()
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 200,
                "transactions": [
                    {
                        "transaction_id": 2000,
                        "sku": "EGG-DRAGON",
                        "quantity": 1,
                        "variations": [
                            {"formatted_name": "Color", "formatted_value": "Red"}
                        ],
                        "personalization": "Happy birthday Ada",
                    }
                ],
            },
        )
        await db.commit()

        body = (
            await signed_in.get(f"/api/products/{egg.id}/observed-options")
        ).json()
        assert [o["name"] for o in body["options"]] == ["Color"]

    async def test_a_product_nobody_has_ordered_reports_nothing(self, signed_in, db):
        product = await _product(db, "NEW-THING")
        await db.commit()
        body = (
            await signed_in.get(f"/api/products/{product.id}/observed-options")
        ).json()
        assert body["options"] == []
