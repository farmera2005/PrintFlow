"""Orders typed in by hand.

The claim being tested is that there is only one pipeline. A phone order is an
`Order` row like any other, so its bundles explode, its stock is decided and
its plates are planned by the same code a polled order runs — and everything
after intake, the board included, needs to know nothing about where it came
from.

What genuinely differs is that nothing will ever look at a typed order again.
A polled order is re-read on every poll, which is how its address gets
backfilled and its fees arrive days later; here there is no second look, so
what was typed is the whole truth. That is why the money is stored on the order
rather than parsed back out of a payload, why each line carries its own price,
and why a blank price has to keep meaning "no price" rather than turning into a
zero somebody would read as a decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog, BomLine, Order, OrderLine, Product
from app.services import books, finance, manual_orders, shipping
from app.services.manual_orders import ManualOrderError

pytestmark = pytest.mark.asyncio


class FakeQbo:
    """Enough QuickBooks to answer "how many have you got?"."""

    def __init__(self, quantities: dict[str, float]):
        self.quantities = quantities

    async def get_items(self, item_ids):
        wanted = {str(i) for i in item_ids}
        return {
            item_id: {"Id": item_id, "TrackQtyOnHand": True, "QtyOnHand": qty}
            for item_id, qty in self.quantities.items()
            if item_id in wanted
        }


@pytest.fixture
def fake_qbo(monkeypatch):
    fake = FakeQbo({"QBO-T": 10})

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)
    return fake


async def seed_catalog(db) -> dict[str, Product]:
    """A bundle of two printed parts, and something sold from a shelf."""
    bundle = Product(sku="GIFT-SET", name="Gift set", fulfillment="bundle")
    part = Product(sku="PART-X", name="Printed part X", fulfillment="printed")
    trinket = Product(
        sku="TRINKET", name="Stocked trinket", fulfillment="stocked", qbo_item_id="QBO-T"
    )
    db.add_all([bundle, part, trinket])
    await db.flush()
    db.add(BomLine(bundle_id=bundle.id, component_id=part.id, quantity=2))
    await db.commit()
    return {"bundle": bundle, "part": part, "trinket": trinket}


def payload(**overrides) -> dict:
    base = {
        "order_number": None,
        "buyer_name": "Dana Buyer",
        "placed_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        "lines": [],
    }
    base.update(overrides)
    return base


async def lines_of(db, order) -> list[OrderLine]:
    return list(
        (
            await db.execute(
                select(OrderLine)
                .where(OrderLine.order_id == order.id)
                .order_by(OrderLine.created_at)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# The same pipeline
# --------------------------------------------------------------------------


class TestCreating:
    async def test_a_typed_order_goes_through_intake_like_any_other(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[
                    {"product_id": catalog["bundle"].id, "quantity": 3,
                     "unit_price": "18.00"},
                    {"product_id": catalog["trinket"].id, "quantity": 2,
                     "unit_price": "4.50"},
                ]
            ),
        )
        await db.commit()

        assert order.source == "manual"
        by_sku = {line.sku_raw: line for line in await lines_of(db, order)}
        # The bundle exploded into its components — 3 × 2 of the printed part —
        # which is intake's doing and not this module's.
        assert by_sku["GIFT-SET"].state == "exploded"
        assert by_sku["PART-X"].quantity == 6
        assert by_sku["PART-X"].qty_to_print == 6
        # And the shelf item was decided against QuickBooks' count of 10.
        assert (by_sku["TRINKET"].qty_from_stock, by_sku["TRINKET"].state) == (2, "ready")
        # And its status came from the same rules: New, because the printed
        # part has no plate to queue yet, rather than anything to do with the
        # order having been typed.
        assert order.status == "new"

    async def test_a_line_is_matched_by_construction_rather_than_by_sku(
        self, db, fake_qbo
    ):
        """The product was picked from a list, so there is nothing to guess at."""
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db, payload(lines=[{"product_id": catalog["trinket"].id, "quantity": 1}])
        )
        await db.commit()

        line = (await lines_of(db, order))[0]
        assert line.product_id == catalog["trinket"].id
        # Still filled in, because the drawer and the audit trail read them.
        assert (line.sku_raw, line.title) == ("TRINKET", "Stocked trinket")

    async def test_the_takings_are_worked_out_rather_than_typed_again(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[
                    {"product_id": catalog["trinket"].id, "quantity": 3,
                     "unit_price": "4.50"}
                ],
                shipping_total="6.95",
                tax_total="1.10",
                discount_total="2.00",
            ),
        )
        await db.commit()

        assert order.items_total == Decimal("13.50")
        assert order.revenue == Decimal("19.55")

    async def test_a_blank_figure_is_not_a_zero(self, db, fake_qbo):
        """"No tax was charged" and "tax has not been filled in" differ, and
        only one of them should print as 0.00 on an invoice."""
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[{"product_id": catalog["trinket"].id, "quantity": 1,
                        "unit_price": "4.50"}],
                shipping_total="",
                tax_total=None,
            ),
        )
        await db.commit()

        assert order.shipping_total is None
        assert order.tax_total is None
        assert order.revenue == Decimal("4.50")

    async def test_a_free_replacement_is_a_real_order_with_nothing_to_bill(
        self, db, fake_qbo
    ):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db, payload(lines=[{"product_id": catalog["part"].id, "quantity": 1}])
        )
        await db.commit()

        assert order.revenue is None
        assert order.items_total is None
        # It still gets made: a replacement has to be printed like anything else.
        assert (await lines_of(db, order))[0].qty_to_print == 1


class TestReferences:
    async def test_one_is_generated_when_none_is_given(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        line = {"product_id": catalog["trinket"].id, "quantity": 1}

        first = await manual_orders.create_order(db, payload(lines=[line]))
        await db.commit()
        second = await manual_orders.create_order(db, payload(lines=[line]))
        await db.commit()

        assert (first.order_number, second.order_number) == ("M-1001", "M-1002")

    async def test_it_counts_from_the_highest_rather_than_from_how_many(
        self, db, fake_qbo
    ):
        """Otherwise cancelling one hands its number out a second time."""
        catalog = await seed_catalog(db)
        line = {"product_id": catalog["trinket"].id, "quantity": 1}
        await manual_orders.create_order(
            db, payload(order_number="M-1007", lines=[line])
        )
        await db.commit()

        assert await manual_orders.next_reference(db) == "M-1008"

    async def test_a_reference_somebody_typed_is_kept_as_typed(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                order_number="  STALL-14 ",
                lines=[{"product_id": catalog["trinket"].id, "quantity": 1}],
            ),
        )
        await db.commit()

        assert order.order_number == "STALL-14"
        # It doubles as the channel id, which is what makes it unique.
        assert order.external_id == "STALL-14"

    async def test_a_reference_that_is_not_a_number_does_not_break_the_next_one(
        self, db, fake_qbo
    ):
        catalog = await seed_catalog(db)
        await manual_orders.create_order(
            db,
            payload(
                order_number="M-STALL",
                lines=[{"product_id": catalog["trinket"].id, "quantity": 1}],
            ),
        )
        await db.commit()

        assert await manual_orders.next_reference(db) == "M-1001"


class TestRefusals:
    """Somebody is filling in a form, so every refusal names the box."""

    async def test_an_order_with_no_lines_is_refused(self, db, fake_qbo):
        with pytest.raises(ManualOrderError, match="at least one line"):
            await manual_orders.create_order(db, payload(lines=[]))

    async def test_a_price_that_is_not_a_number_names_its_line(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        with pytest.raises(ManualOrderError, match="price on line 2"):
            await manual_orders.create_order(
                db,
                payload(
                    lines=[
                        {"product_id": catalog["trinket"].id, "quantity": 1,
                         "unit_price": "4.50"},
                        {"product_id": catalog["trinket"].id, "quantity": 1,
                         "unit_price": "four fifty"},
                    ]
                ),
            )

    async def test_money_going_the_wrong_way_is_refused(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        with pytest.raises(ManualOrderError, match="Shipping cannot be negative"):
            await manual_orders.create_order(
                db,
                payload(
                    lines=[{"product_id": catalog["trinket"].id, "quantity": 1}],
                    shipping_total="-6.95",
                ),
            )

    async def test_a_quantity_of_none_is_refused(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        with pytest.raises(ManualOrderError, match="at least one"):
            await manual_orders.create_order(
                db, payload(lines=[{"product_id": catalog["trinket"].id, "quantity": 0}])
            )

    async def test_a_product_that_no_longer_exists_is_refused(self, db, fake_qbo):
        with pytest.raises(ManualOrderError, match="no longer exists"):
            await manual_orders.create_order(
                db, payload(lines=[{"product_id": uuid.uuid4(), "quantity": 1}])
            )

    async def test_a_bad_third_line_leaves_no_half_an_order_behind(self, db, fake_qbo):
        """Everything is read and checked before anything is added."""
        catalog = await seed_catalog(db)
        good = {"product_id": catalog["trinket"].id, "quantity": 1,
                "unit_price": "4.50"}
        with pytest.raises(ManualOrderError):
            await manual_orders.create_order(
                db, payload(lines=[good, good, {**good, "unit_price": "-1"}])
            )
        await db.rollback()

        assert (await db.execute(select(Order))).scalars().all() == []


# --------------------------------------------------------------------------
# Nothing is ever going to look at it again
# --------------------------------------------------------------------------


class TestNoSecondLook:
    async def test_the_backfill_leaves_a_typed_order_alone(self, db, fake_qbo):
        """It runs on every poll and rewrites the money from the payload.

        A typed order has no payload deliberately, so there is nothing for it
        to read — and the figures somebody entered stay entered.
        """
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[{"product_id": catalog["trinket"].id, "quantity": 1,
                        "unit_price": "4.50"}],
                shipping_total="6.95",
            ),
        )
        await db.commit()

        assert await finance.backfill(db) == {"looked_at": 0, "filled": 0}
        await db.commit()
        assert order.revenue == Decimal("11.45")
        assert order.shipping_total == Decimal("6.95")


class TestInvoicing:
    async def test_the_typed_price_is_what_gets_billed(self, db, fake_qbo):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[{"product_id": catalog["trinket"].id, "quantity": 3,
                        "unit_price": "4.50"}]
            ),
        )
        await db.commit()

        billable = books.invoice_lines(order, await lines_of(db, order))
        assert [(quantity, price) for _, quantity, price in billable] == [
            (3, Decimal("4.50"))
        ]

    async def test_a_line_with_no_price_is_left_off_rather_than_billed_at_zero(
        self, db, fake_qbo
    ):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[
                    {"product_id": catalog["trinket"].id, "quantity": 1,
                     "unit_price": "4.50"},
                    # Sent free alongside it.
                    {"product_id": catalog["part"].id, "quantity": 1},
                ]
            ),
        )
        await db.commit()

        billable = books.invoice_lines(order, await lines_of(db, order))
        assert [line.sku_raw for line, _, _ in billable] == ["TRINKET"]

    async def test_a_price_stored_on_a_polled_line_wins_over_the_receipt(self, db):
        """The same field, used to correct a polled order rather than to type one.

        Worth pinning because it is the reason the price lives on the line for
        every source instead of only for manual ones.
        """
        order = Order(
            source="etsy",
            etsy_receipt_id=4242,
            order_number="4242",
            raw={
                "transactions": [
                    {
                        "transaction_id": 7,
                        "price": {"amount": 1800, "divisor": 100, "currency_code": "USD"},
                    }
                ]
            },
        )
        db.add(order)
        await db.flush()
        line = OrderLine(
            order_id=order.id, etsy_transaction_id=7, title="Gift set", quantity=1
        )
        db.add(line)
        await db.flush()

        assert books.line_unit_price(order, line) == Decimal("18.00")
        line.unit_price = Decimal("15.00")
        assert books.line_unit_price(order, line) == Decimal("15.00")


# --------------------------------------------------------------------------
# Getting it out of the door
# --------------------------------------------------------------------------


class FakeShipStation:
    """A ShipStation that records what it was asked to create."""

    def __init__(self, *, order_id=9911):
        self.order_id = order_id
        self.created: list[dict] = []
        self.looked_up: list[str] = []

    async def create_order(self, body):
        self.created.append(body)
        return {"orderId": self.order_id, "orderNumber": body.get("orderNumber")}

    async def find_order_by_number(self, order_number):
        self.looked_up.append(order_number)
        return None


@pytest.fixture
def fake_shipstation(monkeypatch):
    fake = FakeShipStation()

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.shipping.ss_api.client_for", client_for)
    return fake


TOLEDO = {
    "name": "Dana Buyer",
    "first_line": "12 Kiln Row",
    "second_line": "Unit 4",
    "city": "Toledo",
    "state": "OH",
    "zip": "43601",
    "country": "US",
}


async def _shippable(db, catalog, **overrides):
    order = await manual_orders.create_order(
        db,
        payload(
            lines=[{"product_id": catalog["trinket"].id, "quantity": 2,
                    "unit_price": "4.50"}],
            shipping_total="6.95",
            **{"ship_to": TOLEDO, **overrides},
        ),
    )
    await db.commit()
    return order


class TestSendingItToShipStation:
    async def test_a_typed_order_is_sent_rather_than_looked_for(
        self, db, fake_qbo, fake_shipstation
    ):
        """Nothing is ever going to import it, so waiting to be matched would
        be waiting for good."""
        catalog = await seed_catalog(db)
        order = await _shippable(db, catalog)

        remote_id = await shipping.send_to_shipstation(db, order)

        assert remote_id == 9911
        assert order.shipstation_order_id == 9911
        assert fake_shipstation.looked_up == []
        body = fake_shipstation.created[0]
        assert body["orderNumber"] == "M-1001"
        assert body["orderStatus"] == "awaiting_shipment"
        assert body["shipTo"]["postalCode"] == "43601"
        assert body["shipTo"]["street2"] == "Unit 4"
        assert body["items"] == [
            {"sku": "TRINKET", "name": "Stocked trinket", "quantity": 2,
             "unitPrice": 4.5}
        ]
        assert body["amountPaid"] == 15.95

    async def test_the_key_is_the_order_so_a_repeat_is_not_a_second_parcel(
        self, db, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        order = await _shippable(db, catalog)

        await shipping.send_to_shipstation(db, order)
        # Sent again — the id is already known, so nothing goes out at all.
        await shipping.send_to_shipstation(db, order)

        assert len(fake_shipstation.created) == 1
        # And the key ShipStation dedupes on is the order itself, so even a
        # retry it never heard the answer to updates rather than duplicates.
        assert fake_shipstation.created[0]["orderKey"] == str(order.id)

    async def test_a_bundle_is_one_item_rather_than_its_parts(
        self, db, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db,
            payload(
                lines=[{"product_id": catalog["bundle"].id, "quantity": 1,
                        "unit_price": "18.00"}],
                ship_to=TOLEDO,
            ),
        )
        await db.commit()

        await shipping.send_to_shipstation(db, order)

        assert [item["sku"] for item in fake_shipstation.created[0]["items"]] == [
            "GIFT-SET"
        ]

    async def test_an_order_with_nowhere_to_go_says_what_is_missing(
        self, db, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        order = await manual_orders.create_order(
            db, payload(lines=[{"product_id": catalog["trinket"].id, "quantity": 1}])
        )
        await db.commit()

        with pytest.raises(shipping.LabelError, match="a street address"):
            await shipping.send_to_shipstation(db, order)
        assert fake_shipstation.created == []

    async def test_a_country_shipstation_cannot_read_is_caught_before_the_call(
        self, db, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        order = await _shippable(
            db, catalog, ship_to={**TOLEDO, "country": "United States"}
        )

        with pytest.raises(shipping.LabelError, match="two-letter country code"):
            await shipping.send_to_shipstation(db, order)

    async def test_a_us_address_with_no_state_is_caught_too(
        self, db, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        order = await _shippable(db, catalog, ship_to={**TOLEDO, "state": ""})

        with pytest.raises(shipping.LabelError, match="needs a state"):
            await shipping.send_to_shipstation(db, order)

    async def test_the_sweep_sends_the_typed_ones_and_matches_the_rest(
        self, db, fake_qbo, fake_shipstation
    ):
        """One sweep, one job: after it, everything shippable has a ShipStation
        order behind it."""
        catalog = await seed_catalog(db)
        typed = await _shippable(db, catalog)
        collected = await manual_orders.create_order(
            db,
            payload(
                order_number="STALL-1",
                lines=[{"product_id": catalog["trinket"].id, "quantity": 1}],
            ),
        )
        polled = Order(
            source="etsy", etsy_receipt_id=8080, order_number="8080", status="new"
        )
        db.add(polled)
        await db.commit()

        stats = await shipping.match_orders(db)
        await db.commit()

        assert typed.shipstation_order_id == 9911
        # Nowhere to send it, which is what an order being collected looks
        # like — counted apart from a failure to match.
        assert collected.shipstation_order_id is None
        assert (stats["sent"], stats["no_address"], stats["not_found"]) == (1, 1, 1)
        # And the polled one was looked for, the way it always was.
        assert fake_shipstation.looked_up == ["8080"]


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------


class TestThroughTheApi:
    async def test_an_order_can_be_typed_in_and_lands_on_the_list(
        self, db, signed_in, fake_qbo
    ):
        catalog = await seed_catalog(db)
        response = await signed_in.post(
            "/api/orders",
            json={
                "buyer_name": "Dana Buyer",
                "placed_at": "2026-09-01T12:00:00Z",
                "shipping_total": "6.95",
                "ship_to": {"name": "Dana Buyer", "city": "Toledo", "state": "OH"},
                "lines": [
                    {"product_id": str(catalog["trinket"].id), "quantity": 2,
                     "unit_price": "4.50"}
                ],
            },
        )
        assert response.status_code == 201, response.text
        detail = response.json()
        # The same shape the drawer loads, so it can be opened straight away.
        assert detail["order_number"] == "M-1001"
        assert detail["source"] == "manual"
        assert detail["ship_to"]["city"] == "Toledo"

        listed = (await signed_in.get("/api/orders")).json()
        assert [order["order_number"] for order in listed["orders"]] == ["M-1001"]

        # And it says who typed it, which a polled order has no need of.
        actions = (
            (await db.execute(select(AuditLog.action, AuditLog.actor))).all()
        )
        assert ("manual_order_created", "admin") in actions

    async def test_the_same_reference_twice_is_a_conflict_not_a_crash(
        self, db, signed_in, fake_qbo
    ):
        catalog = await seed_catalog(db)
        body = {
            "order_number": "STALL-14",
            "lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}],
        }
        assert (await signed_in.post("/api/orders", json=body)).status_code == 201

        clash = await signed_in.post("/api/orders", json=body)
        assert clash.status_code == 409
        assert "STALL-14" in clash.json()["detail"]

    async def test_a_bad_figure_comes_back_as_something_to_read(
        self, db, signed_in, fake_qbo
    ):
        catalog = await seed_catalog(db)
        response = await signed_in.post(
            "/api/orders",
            json={
                "tax_total": "lots",
                "lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}],
            },
        )
        assert response.status_code == 400
        assert "Tax is not a number" in response.json()["detail"]
        # And nothing was left behind by the attempt.
        assert (await db.execute(select(Order))).scalars().all() == []

    async def test_the_match_button_sends_a_typed_order_across(
        self, db, signed_in, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        created = await signed_in.post(
            "/api/orders",
            json={
                "ship_to": TOLEDO,
                "lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1,
                           "unit_price": "4.50"}],
            },
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

        response = await signed_in.post(f"/api/orders/{order_id}/match-shipstation")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": True,
            "sent": True,
            "shipstation_order_id": 9911,
        }
        assert fake_shipstation.looked_up == []

    async def test_an_order_with_no_address_is_told_so_rather_than_sent(
        self, db, signed_in, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        created = await signed_in.post(
            "/api/orders",
            json={"lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}]},
        )
        order_id = created.json()["id"]

        response = await signed_in.post(f"/api/orders/{order_id}/match-shipstation")
        assert response.status_code == 400
        assert "street address" in response.json()["detail"]

    async def test_an_address_can_be_typed_in_afterwards(
        self, db, signed_in, fake_qbo, fake_shipstation
    ):
        """The address is often the part somebody has to go and ask for."""
        catalog = await seed_catalog(db)
        created = await signed_in.post(
            "/api/orders",
            json={"lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}]},
        )
        order_id = created.json()["id"]

        saved = await signed_in.patch(
            f"/api/orders/{order_id}/ship-to", json=TOLEDO
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["ship_to"]["zip"] == "43601"
        assert saved.json()["note"] is None

        # And now it can go.
        sent = await signed_in.post(f"/api/orders/{order_id}/match-shipstation")
        assert sent.status_code == 200, sent.text

    async def test_a_correction_is_carried_to_shipstation_under_the_same_key(
        self, db, signed_in, fake_qbo, fake_shipstation
    ):
        catalog = await seed_catalog(db)
        created = await signed_in.post(
            "/api/orders",
            json={
                "ship_to": TOLEDO,
                "lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}],
            },
        )
        order_id = created.json()["id"]
        await signed_in.post(f"/api/orders/{order_id}/match-shipstation")

        fixed = await signed_in.patch(
            f"/api/orders/{order_id}/ship-to",
            json={**TOLEDO, "first_line": "14 Kiln Row"},
        )
        assert fixed.status_code == 200, fixed.text
        assert len(fake_shipstation.created) == 2
        first, second = fake_shipstation.created
        # The same order, corrected — not a second parcel to pack.
        assert first["orderKey"] == second["orderKey"]
        assert second["shipTo"]["street1"] == "14 Kiln Row"

    async def test_a_polled_order_s_address_is_not_ours_to_edit(self, db, signed_in):
        order = Order(source="etsy", etsy_receipt_id=5150, order_number="5150")
        db.add(order)
        await db.commit()

        response = await signed_in.patch(
            f"/api/orders/{order.id}/ship-to", json=TOLEDO
        )
        assert response.status_code == 400
        assert "re-read on every poll" in response.json()["detail"]

    async def test_an_address_a_label_was_bought_for_is_left_alone(
        self, db, signed_in, fake_qbo
    ):
        catalog = await seed_catalog(db)
        created = await signed_in.post(
            "/api/orders",
            json={
                "ship_to": TOLEDO,
                "lines": [{"product_id": str(catalog["trinket"].id), "quantity": 1}],
            },
        )
        order = await db.get(Order, uuid.UUID(created.json()["id"]))
        order.label_created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        await db.commit()

        response = await signed_in.patch(
            f"/api/orders/{order.id}/ship-to", json={**TOLEDO, "city": "Elsewhere"}
        )
        assert response.status_code == 400
        assert "already been bought" in response.json()["detail"]

    async def test_an_order_with_no_lines_is_refused_by_the_schema(
        self, db, signed_in
    ):
        response = await signed_in.post("/api/orders", json={"lines": []})
        assert response.status_code == 422

    async def test_it_takes_a_signed_in_user(self, db, client):
        response = await client.post(
            "/api/orders",
            json={"lines": [{"product_id": str(uuid.uuid4()), "quantity": 1}]},
        )
        assert response.status_code == 401
