"""Matching an Etsy listing to a product when the listing carries no SKU.

Etsy does not require sellers to fill in a SKU, and a shop that never bothered
has nothing for intake to look up — every order lands unmatched and stays there.
The fix is to match on what Etsy always sends: the listing id, and the product
id of the exact variation the buyer bought.

Two things these tests are careful about:

* **Order of preference.** A real SKU still wins. A link is a local decision
  about a listing, and if the seller later fills the SKU in, the SKU takes over
  without anyone having to remove the link.
* **The listing beats the variation.** Etsy issues a new product id for a
  variation every time the seller edits the listing's options, so a
  variation-scoped link is the fragile one and only ever applies when it matches
  exactly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import EtsyProductLink, OrderLine, Product
from app.services import intake

pytestmark = pytest.mark.asyncio

LISTING = 1_895_497_697
VARIANT_A = 26_682_511_648
VARIANT_B = 26_682_511_999


async def _product(session, sku, fulfillment="stocked"):
    product = Product(sku=sku, name=sku, fulfillment=fulfillment, qbo_item_id=None)
    session.add(product)
    await session.flush()
    await session.refresh(product)
    return product


def _receipt(receipt_id, *, sku="", listing_id=LISTING, product_id=VARIANT_A):
    """A receipt shaped like the ones this shop actually gets: no SKU."""
    return {
        "receipt_id": receipt_id,
        "name": "Ada",
        "created_timestamp": 1_780_000_000,
        "transactions": [
            {
                "transaction_id": receipt_id * 10,
                "listing_id": listing_id,
                "product_id": product_id,
                "sku": sku,
                "title": "Bin with a fan",
                "quantity": 1,
                "variations": [{"formatted_name": "Bin Fan", "formatted_value": "No"}],
            }
        ],
    }


async def _line(session, order):
    return (
        await session.execute(
            select(OrderLine).where(
                OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# What intake stores
# --------------------------------------------------------------------------


class TestCapturingTheIdentity:
    async def test_the_variation_id_is_kept_on_the_line(self, db):
        order, _ = await intake.ingest_receipt(db, _receipt(1))
        await db.commit()
        line = await _line(db, order)
        assert line.etsy_listing_id == LISTING
        assert line.etsy_product_id == VARIANT_A

    async def test_an_older_line_gains_the_variation_id_on_the_next_poll(self, db):
        """Orders taken before this existed have the id in their stored payload."""
        order, _ = await intake.ingest_receipt(db, _receipt(2))
        await db.commit()
        line = await _line(db, order)
        line.etsy_product_id = None
        await db.commit()

        # Same receipt again — what a poll does every few minutes.
        await intake.ingest_receipt(db, _receipt(2))
        await db.commit()
        await db.refresh(line)
        assert line.etsy_product_id == VARIANT_A

    async def test_a_repeat_poll_of_a_complete_line_does_nothing(self, db, monkeypatch):
        """Every open order takes this path on every poll; it must stay cheap."""
        await intake.ingest_receipt(db, _receipt(9))
        await db.commit()

        calls = {"n": 0}
        original = intake.process_order

        async def counted(session, order):
            calls["n"] += 1
            return await original(session, order)

        monkeypatch.setattr(intake, "process_order", counted)
        await intake.ingest_receipt(db, _receipt(9))
        assert calls["n"] == 0


# --------------------------------------------------------------------------
# The matching cascade
# --------------------------------------------------------------------------


class TestMatching:
    async def test_no_sku_and_no_link_stays_unmatched(self, db):
        """The state of the world before any of this: nothing to match on."""
        await _product(db, "BIN-FAN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(3))
        await db.commit()
        line = await _line(db, order)
        assert line.state == "unmatched"
        assert line.product_id is None

    async def test_a_listing_link_matches(self, db):
        bin_ = await _product(db, "BIN-FAN")
        db.add(EtsyProductLink(product_id=bin_.id, etsy_listing_id=LISTING))
        await db.commit()

        order, _ = await intake.ingest_receipt(db, _receipt(4))
        await db.commit()
        line = await _line(db, order)
        assert line.product_id == bin_.id
        assert line.state != "unmatched"

    async def test_a_variation_link_beats_a_listing_link(self, db):
        general = await _product(db, "BIN-PLAIN")
        specific = await _product(db, "BIN-FAN")
        db.add(EtsyProductLink(product_id=general.id, etsy_listing_id=LISTING))
        db.add(
            EtsyProductLink(
                product_id=specific.id,
                etsy_listing_id=LISTING,
                etsy_product_id=VARIANT_A,
            )
        )
        await db.commit()

        order, _ = await intake.ingest_receipt(db, _receipt(5, product_id=VARIANT_A))
        await db.commit()
        assert (await _line(db, order)).product_id == specific.id

    async def test_another_variation_falls_back_to_the_listing(self, db):
        general = await _product(db, "BIN-PLAIN")
        specific = await _product(db, "BIN-FAN")
        db.add(EtsyProductLink(product_id=general.id, etsy_listing_id=LISTING))
        db.add(
            EtsyProductLink(
                product_id=specific.id,
                etsy_listing_id=LISTING,
                etsy_product_id=VARIANT_A,
            )
        )
        await db.commit()

        order, _ = await intake.ingest_receipt(db, _receipt(6, product_id=VARIANT_B))
        await db.commit()
        assert (await _line(db, order)).product_id == general.id

    async def test_a_real_sku_wins_over_a_link(self, db):
        """A link is a local guess; a SKU is what the seller actually declared."""
        by_sku = await _product(db, "BIN-FAN")
        by_link = await _product(db, "BIN-PLAIN")
        db.add(EtsyProductLink(product_id=by_link.id, etsy_listing_id=LISTING))
        await db.commit()

        order, _ = await intake.ingest_receipt(db, _receipt(7, sku="BIN-FAN"))
        await db.commit()
        assert (await _line(db, order)).product_id == by_sku.id

    async def test_a_link_for_a_different_listing_does_not_match(self, db):
        other = await _product(db, "SOMETHING-ELSE")
        db.add(EtsyProductLink(product_id=other.id, etsy_listing_id=LISTING + 1))
        await db.commit()

        order, _ = await intake.ingest_receipt(db, _receipt(8))
        await db.commit()
        assert (await _line(db, order)).state == "unmatched"


# --------------------------------------------------------------------------
# Linking from the order drawer
# --------------------------------------------------------------------------


class TestRememberingFromAnOrder:
    async def test_linking_without_remember_fixes_only_this_line(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        first, _ = await intake.ingest_receipt(db, _receipt(10))
        second, _ = await intake.ingest_receipt(db, _receipt(11))
        await db.commit()

        line = await _line(db, first)
        response = await signed_in.post(
            f"/api/orders/{first.id}/lines/{line.id}/link-product",
            json={"product_id": str(bin_.id)},
        )
        assert response.status_code == 200
        assert response.json()["also_fixed"] == 0

        assert (await db.execute(select(EtsyProductLink))).scalars().all() == []
        await db.refresh(await _line(db, second))
        assert (await _line(db, second)).state == "unmatched"

    async def test_remembering_clears_the_backlog(self, signed_in, db):
        """The reason to remember: the listing has already sold more than once."""
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        first, _ = await intake.ingest_receipt(db, _receipt(12))
        second, _ = await intake.ingest_receipt(db, _receipt(13))
        third, _ = await intake.ingest_receipt(db, _receipt(14))
        await db.commit()

        line = await _line(db, first)
        body = (
            await signed_in.post(
                f"/api/orders/{first.id}/lines/{line.id}/link-product",
                json={"product_id": str(bin_.id), "remember": True},
            )
        ).json()
        # The line being linked was matched before the rule ran, so the count is
        # the other two.
        assert body["also_fixed"] == 2

        for order in (second, third):
            other = await _line(db, order)
            await db.refresh(other)
            assert other.product_id == bin_.id
            assert other.state != "unmatched"

    async def test_remembering_covers_the_whole_listing_by_default(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(15))
        await db.commit()

        line = await _line(db, order)
        await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/link-product",
            json={"product_id": str(bin_.id), "remember": True},
        )
        link = (await db.execute(select(EtsyProductLink))).scalar_one()
        assert link.etsy_listing_id == LISTING
        assert link.etsy_product_id is None

        # A different variation of the same listing now matches too.
        later, _ = await intake.ingest_receipt(db, _receipt(16, product_id=VARIANT_B))
        await db.commit()
        assert (await _line(db, later)).product_id == bin_.id

    async def test_a_variation_scope_pins_to_that_variation(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(17))
        await db.commit()

        line = await _line(db, order)
        await signed_in.post(
            f"/api/orders/{order.id}/lines/{line.id}/link-product",
            json={
                "product_id": str(bin_.id),
                "remember": True,
                "remember_scope": "variant",
            },
        )
        link = (await db.execute(select(EtsyProductLink))).scalar_one()
        assert link.etsy_product_id == VARIANT_A

        later, _ = await intake.ingest_receipt(db, _receipt(18, product_id=VARIANT_B))
        await db.commit()
        assert (await _line(db, later)).state == "unmatched"

    async def test_relinking_moves_the_link_rather_than_failing(self, signed_in, db):
        """Correcting a wrong link must not need the old one deleted first."""
        wrong = await _product(db, "WRONG")
        right = await _product(db, "RIGHT")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(19))
        await db.commit()

        line = await _line(db, order)
        for product in (wrong, right):
            response = await signed_in.post(
                f"/api/orders/{order.id}/lines/{line.id}/link-product",
                json={"product_id": str(product.id), "remember": True},
            )
            assert response.status_code == 200

        link = (await db.execute(select(EtsyProductLink))).scalar_one()
        assert link.product_id == right.id


# --------------------------------------------------------------------------
# Linking from the Products screen
# --------------------------------------------------------------------------


class TestLinksOnAProduct:
    async def test_a_link_is_listed_on_the_product(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()

        created = await signed_in.post(
            f"/api/products/{bin_.id}/etsy-links",
            json={"etsy_listing_id": LISTING, "listing_title": "Bin with a fan"},
        )
        assert created.status_code == 201
        body = created.json()
        assert [link["etsy_listing_id"] for link in body["etsy_links"]] == [LISTING]
        assert body["etsy_links"][0]["listing_title"] == "Bin with a fan"

    async def test_linking_by_hand_clears_waiting_orders(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        order, _ = await intake.ingest_receipt(db, _receipt(20))
        await db.commit()
        assert (await _line(db, order)).state == "unmatched"

        body = (
            await signed_in.post(
                f"/api/products/{bin_.id}/etsy-links",
                json={"etsy_listing_id": LISTING},
            )
        ).json()
        assert body["also_fixed"] == 1

        line = await _line(db, order)
        await db.refresh(line)
        assert line.product_id == bin_.id

    async def test_one_listing_cannot_point_at_two_products(self, signed_in, db):
        first = await _product(db, "BIN-FAN")
        second = await _product(db, "BIN-PLAIN")
        await db.commit()

        assert (
            await signed_in.post(
                f"/api/products/{first.id}/etsy-links",
                json={"etsy_listing_id": LISTING},
            )
        ).status_code == 201
        clash = await signed_in.post(
            f"/api/products/{second.id}/etsy-links", json={"etsy_listing_id": LISTING}
        )
        assert clash.status_code == 409
        assert "already linked" in clash.json()["detail"]

    async def test_removing_a_link_stops_new_orders_matching(self, signed_in, db):
        bin_ = await _product(db, "BIN-FAN")
        await db.commit()
        created = (
            await signed_in.post(
                f"/api/products/{bin_.id}/etsy-links",
                json={"etsy_listing_id": LISTING},
            )
        ).json()
        link_id = created["etsy_links"][0]["id"]

        removed = await signed_in.delete(
            f"/api/products/{bin_.id}/etsy-links/{link_id}"
        )
        assert removed.status_code == 200
        assert removed.json()["etsy_links"] == []

        order, _ = await intake.ingest_receipt(db, _receipt(21))
        await db.commit()
        assert (await _line(db, order)).state == "unmatched"


# --------------------------------------------------------------------------
# What the board shows
# --------------------------------------------------------------------------


class TestOnTheBoard:
    async def test_the_line_carries_its_etsy_identity(self, signed_in, db):
        """The drawer needs it to offer "remember this listing"."""
        order, _ = await intake.ingest_receipt(db, _receipt(22))
        await db.commit()

        body = (await signed_in.get(f"/api/orders/{order.id}")).json()
        line = body["lines"][0]
        assert line["etsy_listing_id"] == LISTING
        assert line["etsy_product_id"] == VARIANT_A


# --------------------------------------------------------------------------
# What the catalogue screen says
# --------------------------------------------------------------------------


class TestCatalogue:
    async def test_a_linked_listing_is_not_reported_as_a_problem(self, db):
        from app.services import catalog

        bin_ = await _product(db, "BIN-FAN")
        db.add(EtsyProductLink(product_id=bin_.id, etsy_listing_id=LISTING))
        await db.commit()

        result = await catalog.reconcile(
            db,
            [
                {
                    "listing_id": LISTING,
                    "title": "Bin with a fan",
                    "state": "active",
                    "inventory": {
                        "products": [{"product_id": VARIANT_A, "sku": "", "property_values": []}]
                    },
                }
            ],
        )
        row = result["rows"][0]
        assert row["status"] == "linked"
        assert row["product_sku"] == "BIN-FAN"
        assert result["counts"]["no_sku"] == 0

    async def test_an_unlinked_listing_with_no_sku_is_still_reported(self, db):
        from app.services import catalog

        result = await catalog.reconcile(
            db,
            [
                {
                    "listing_id": LISTING,
                    "title": "Bin with a fan",
                    "state": "active",
                    "inventory": {
                        "products": [{"product_id": VARIANT_A, "sku": "", "property_values": []}]
                    },
                }
            ],
        )
        assert result["rows"][0]["status"] == "no_sku"
