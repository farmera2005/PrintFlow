"""Building products from Etsy listings, for a shop that has never used SKUs.

Linking listings one at a time is fine for a stray order; it is not a way to set
up a shop. This is the bulk path: take the listings Etsy already describes, make
a product from each, and attach the link in the same step so nobody has to
invent a code or remember to link afterwards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import EtsyProductLink, OrderLine, Product
from app.services import catalog, codes, intake

pytestmark = pytest.mark.asyncio

LISTING_A = 1_895_497_697
LISTING_B = 1_895_497_698


def _listing(listing_id, title, *, skus=(""), variants=1):
    """A listing shaped the way `fetch_listings` hands them over."""
    products = [
        {
            "product_id": listing_id * 10 + index,
            "sku": (skus[index] if index < len(skus) else ""),
            "property_values": [],
        }
        for index in range(variants)
    ]
    return {
        "listing_id": listing_id,
        "title": title,
        "state": "active",
        "inventory": {"products": products},
    }


async def _line_states(session):
    return [
        line.state
        for line in (await session.execute(select(OrderLine))).scalars().all()
    ]


# --------------------------------------------------------------------------
# Generated codes
# --------------------------------------------------------------------------


class TestCodes:
    async def test_a_listing_code_can_be_pasted_into_an_etsy_url(self):
        assert codes.from_listing(LISTING_A) == f"ETSY-{LISTING_A}"

    async def test_a_variation_code_names_both_ids(self):
        assert codes.from_listing(LISTING_A, 42) == f"ETSY-{LISTING_A}-42"

    async def test_no_listing_means_no_code(self):
        assert codes.from_listing(None) is None

    async def test_a_name_becomes_a_usable_slug(self):
        assert codes.from_name("Storage bin (with fan!)") == "STORAGE-BIN-WITH-FAN"

    async def test_an_unusable_name_still_yields_something(self):
        assert codes.from_name("   ") == "PRODUCT"

    async def test_a_taken_code_gets_a_suffix(self, db):
        db.add(Product(sku="BIN", name="Bin", fulfillment="stocked", qbo_item_id=None))
        await db.commit()
        assert await codes.unique(db, "BIN") == "BIN-2"

    async def test_suffixes_keep_climbing(self, db):
        for sku in ("BIN", "BIN-2"):
            db.add(Product(sku=sku, name=sku, fulfillment="stocked", qbo_item_id=None))
        await db.commit()
        assert await codes.unique(db, "BIN") == "BIN-3"

    async def test_case_does_not_let_two_codes_collide(self, db):
        """The unique index is on lower(sku), so a suggestion must match it."""
        db.add(Product(sku="bin", name="Bin", fulfillment="stocked", qbo_item_id=None))
        await db.commit()
        assert await codes.unique(db, "BIN") == "BIN-2"


# --------------------------------------------------------------------------
# Importing
# --------------------------------------------------------------------------


class TestImport:
    async def test_a_product_is_created_and_linked(self, db):
        listings = [_listing(LISTING_A, "Storage bin")]
        result = await catalog.import_listings(
            db, listings, [LISTING_A], fulfillment="printed"
        )
        await db.commit()

        assert len(result["created"]) == 1
        product = (await db.execute(select(Product))).scalar_one()
        assert product.name == "Storage bin"
        assert product.fulfillment == "printed"
        assert product.sku == f"ETSY-{LISTING_A}"

        link = (await db.execute(select(EtsyProductLink))).scalar_one()
        assert link.product_id == product.id
        assert link.etsy_listing_id == LISTING_A
        # Listing-wide, not pinned to a variation.
        assert link.etsy_product_id is None
        assert link.listing_title == "Storage bin"

    async def test_one_product_per_listing_however_many_variations(self, db):
        """Variations are option rules, not separate products."""
        listings = [_listing(LISTING_A, "Storage bin", variants=4)]
        await catalog.import_listings(db, listings, [LISTING_A], fulfillment="stocked")
        await db.commit()
        assert len((await db.execute(select(Product))).scalars().all()) == 1

    async def test_a_listing_that_does_have_one_sku_keeps_it_as_the_code(self, db):
        """The shop's own word for the thing beats a generated one."""
        listings = [_listing(LISTING_A, "Storage bin", skus=("BIN-FAN",))]
        await catalog.import_listings(db, listings, [LISTING_A], fulfillment="stocked")
        await db.commit()
        assert (await db.execute(select(Product))).scalar_one().sku == "BIN-FAN"

    async def test_variations_with_different_skus_fall_back_to_the_listing_code(self, db):
        """No single SKU describes the product, so do not pick one at random."""
        listings = [
            _listing(LISTING_A, "Storage bin", skus=("BIN-A", "BIN-B"), variants=2)
        ]
        await catalog.import_listings(db, listings, [LISTING_A], fulfillment="stocked")
        await db.commit()
        assert (await db.execute(select(Product))).scalar_one().sku == f"ETSY-{LISTING_A}"

    async def test_an_already_linked_listing_is_skipped_not_duplicated(self, db):
        listings = [_listing(LISTING_A, "Storage bin")]
        await catalog.import_listings(db, listings, [LISTING_A], fulfillment="stocked")
        await db.commit()

        again = await catalog.import_listings(
            db, listings, [LISTING_A], fulfillment="stocked"
        )
        await db.commit()
        assert again["created"] == []
        assert again["skipped"] == [{"listing_id": LISTING_A, "reason": "already linked"}]
        assert len((await db.execute(select(Product))).scalars().all()) == 1

    async def test_a_listing_etsy_no_longer_returns_is_reported(self, db):
        result = await catalog.import_listings(
            db, [], [LISTING_A], fulfillment="stocked"
        )
        assert result["created"] == []
        assert result["skipped"][0]["reason"] == "Etsy no longer lists it"

    async def test_imported_products_are_not_reported_as_unsold(self, db):
        """Their codes were never meant to appear in Etsy, so do not look there.

        Checking only for the code would flag every product this import makes
        as "nothing on Etsy sells this" the moment it was created.
        """
        listings = [_listing(LISTING_A, "Storage bin")]
        await catalog.import_listings(db, listings, [LISTING_A], fulfillment="stocked")
        await db.commit()

        result = await catalog.reconcile(db, listings)
        assert result["unused_products"] == []
        assert result["counts"]["linked"] == 1

    async def test_a_product_nothing_sells_is_still_reported(self, db):
        db.add(Product(sku="ORPHAN", name="Orphan", fulfillment="stocked", qbo_item_id=None))
        await db.commit()
        result = await catalog.reconcile(db, [_listing(LISTING_A, "Storage bin")])
        assert [row["sku"] for row in result["unused_products"]] == ["ORPHAN"]

    async def test_two_listings_with_the_same_title_get_distinct_codes(self, db):
        listings = [_listing(LISTING_A, "Storage bin"), _listing(LISTING_B, "Storage bin")]
        await catalog.import_listings(
            db, listings, [LISTING_A, LISTING_B], fulfillment="stocked"
        )
        await db.commit()
        made = (await db.execute(select(Product))).scalars().all()
        assert len({product.sku for product in made}) == 2


# --------------------------------------------------------------------------
# Through the API, with orders already waiting
# --------------------------------------------------------------------------


class TestImportEndpoint:
    async def test_it_clears_the_orders_that_were_waiting(self, signed_in, db, monkeypatch):
        """The reason to import at all: orders are already stuck on these."""
        listings = [_listing(LISTING_A, "Storage bin")]

        async def fake_fetch(client, *, state="active"):
            return listings, []

        monkeypatch.setattr(catalog, "fetch_listings", fake_fetch)

        class _Client:
            shop_id = 12345

        async def fake_client(session):
            return _Client()

        from app.routers import integrations_router

        monkeypatch.setattr(integrations_router, "_etsy_client", fake_client)

        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 555,
                "name": "Ada",
                "transactions": [
                    {
                        "transaction_id": 5550,
                        "listing_id": LISTING_A,
                        "product_id": LISTING_A * 10,
                        "sku": "",
                        "title": "Storage bin",
                        "quantity": 1,
                    }
                ],
            },
        )
        await db.commit()
        assert await _line_states(db) == ["unmatched"]

        body = (
            await signed_in.post(
                "/api/integrations/etsy/catalog/import",
                json={"listing_ids": [LISTING_A], "fulfillment": "stocked"},
            )
        ).json()
        assert len(body["created"]) == 1
        assert body["also_fixed"] == 1

        db.expire_all()
        # Which state exactly depends on stock; what matters is that it is no
        # longer waiting for somebody to tell it what it is.
        assert await _line_states(db) == ["ready"]

    async def test_fulfillment_is_checked(self, signed_in):
        response = await signed_in.post(
            "/api/integrations/etsy/catalog/import",
            json={"listing_ids": [LISTING_A], "fulfillment": "bundle"},
        )
        # A bundle has a BOM that only a human can write, so it is not something
        # a bulk import can invent.
        assert response.status_code == 422
