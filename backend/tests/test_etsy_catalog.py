"""Checking what Etsy sells against what PrintFlow can make.

Intake already reports an unmatched SKU, but only once a real order has arrived
and stalled on it. This is the same comparison made up front, so the answer
arrives while there is still time to do something about it.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations import etsy as etsy_api
from app.integrations.base import AuthExpiredError, IntegrationError
from app.models import PROVIDER_ETSY, Product
from app.services import catalog, credentials

pytestmark = pytest.mark.asyncio


def _inventory(*variants):
    """variants: ("SKU", {"Color": "Red"}) ..."""
    return {
        "products": [
            {
                "product_id": 1000 + index,
                "sku": sku,
                "is_deleted": False,
                "property_values": [
                    {"property_id": 200 + i, "property_name": name, "values": [value]}
                    for i, (name, value) in enumerate(options.items())
                ],
            }
            for index, (sku, options) in enumerate(variants)
        ]
    }


def _listing(listing_id, title, inventory=None, skus=None, state="active"):
    listing = {
        "listing_id": listing_id,
        "title": title,
        "state": state,
        "url": f"https://etsy.com/listing/{listing_id}",
    }
    if inventory is not None:
        listing["inventory"] = inventory
    if skus is not None:
        listing["skus"] = skus
    return listing


# --------------------------------------------------------------------------
# Reading a listing
# --------------------------------------------------------------------------


class TestReadingAListing:
    async def test_each_buyable_combination_becomes_a_row(self):
        listing = _listing(
            1,
            "Dragon Egg",
            _inventory(("EGG-RED", {"Color": "Red"}), ("EGG-BLUE", {"Color": "Blue"})),
        )
        variants = etsy_api.listing_variants(
            listing, etsy_api.listing_inventory_of(listing)
        )
        assert [v["sku"] for v in variants] == ["EGG-RED", "EGG-BLUE"]
        assert variants[0]["options"] == [
            {"name": "Color", "value": "Red", "property_id": 200}
        ]

    async def test_the_listings_options_are_collected(self):
        listing = _listing(
            1,
            "Dragon Egg",
            _inventory(
                ("EGG-RED-S", {"Color": "Red", "Size": "Small"}),
                ("EGG-RED-L", {"Color": "Red", "Size": "Large"}),
                ("EGG-BLUE-S", {"Color": "Blue", "Size": "Small"}),
            ),
        )
        variants = etsy_api.listing_variants(
            listing, etsy_api.listing_inventory_of(listing)
        )
        options = etsy_api.listing_options(variants)
        assert options == [
            {"name": "Color", "values": ["Red", "Blue"]},
            {"name": "Size", "values": ["Small", "Large"]},
        ]

    async def test_a_listing_with_no_variations_uses_its_own_sku(self):
        listing = _listing(2, "Plain Egg", skus=["EGG-PLAIN"])
        variants = etsy_api.listing_variants(listing, None)
        assert variants == [{"sku": "EGG-PLAIN", "options": [], "product_id": None}]

    async def test_a_listing_with_no_sku_at_all_still_appears(self):
        """It cannot be matched, and that is exactly what needs saying."""
        variants = etsy_api.listing_variants(_listing(3, "Mystery"), None)
        assert variants == [{"sku": None, "options": [], "product_id": None}]

    async def test_deleted_variants_are_ignored(self):
        listing = _listing(4, "Dragon Egg", _inventory(("EGG-RED", {"Color": "Red"})))
        listing["inventory"]["products"][0]["is_deleted"] = True
        assert etsy_api.listing_variants(listing, listing["inventory"])[0]["sku"] is None

    async def test_inventory_is_found_under_either_spelling(self):
        inline = _inventory(("A", {}))
        assert etsy_api.listing_inventory_of({"inventory": inline}) is inline
        assert etsy_api.listing_inventory_of({"Inventory": inline}) is inline
        assert etsy_api.listing_inventory_of({}) is None


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


async def _product(session, sku, name="Thing", fulfillment="printed"):
    product = Product(sku=sku, name=name, fulfillment=fulfillment)
    session.add(product)
    await session.flush()
    return product


class TestReconciliation:
    async def test_a_known_sku_is_matched(self, db):
        await _product(db, "EGG-RED", "Red Egg")
        listings = [_listing(1, "Dragon Egg", _inventory(("EGG-RED", {"Color": "Red"})))]
        result = await catalog.reconcile(db, listings)

        assert result["counts"]["matched"] == 1
        row = result["rows"][0]
        assert row["status"] == "matched"
        assert row["product_name"] == "Red Egg"

    async def test_an_unknown_sku_is_reported_as_missing(self, db):
        """These are the orders that will stall on arrival."""
        listings = [_listing(1, "Dragon Egg", _inventory(("EGG-RED", {"Color": "Red"})))]
        result = await catalog.reconcile(db, listings)
        assert result["counts"]["missing"] == 1
        assert result["rows"][0]["status"] == "missing"
        assert result["rows"][0]["product_id"] is None

    async def test_matching_ignores_case_and_padding(self, db):
        """The same comparison intake makes, so the two cannot disagree."""
        await _product(db, "egg-red")
        listings = [_listing(1, "Dragon Egg", _inventory((" EGG-RED ", {})))]
        result = await catalog.reconcile(db, listings)
        assert result["rows"][0]["status"] == "matched"

    async def test_a_listing_without_a_sku_is_called_out(self, db):
        listings = [_listing(1, "Mystery")]
        result = await catalog.reconcile(db, listings)
        assert result["counts"]["no_sku"] == 1
        assert result["rows"][0]["status"] == "no_sku"

    async def test_products_no_listing_sells_are_listed_separately(self, db):
        """Usually components; sometimes a typo or a retired listing."""
        await _product(db, "EGG-RED")
        await _product(db, "PLA-GREY", fulfillment="stocked")
        listings = [_listing(1, "Dragon Egg", _inventory(("EGG-RED", {})))]
        result = await catalog.reconcile(db, listings)
        assert [p["sku"] for p in result["unused_products"]] == ["PLA-GREY"]

    async def test_every_variant_gets_its_own_row(self, db):
        await _product(db, "EGG-RED")
        listings = [
            _listing(
                1,
                "Dragon Egg",
                _inventory(("EGG-RED", {"Color": "Red"}), ("EGG-BLUE", {"Color": "Blue"})),
            )
        ]
        result = await catalog.reconcile(db, listings)
        assert result["counts"]["variants"] == 2
        assert result["counts"]["listings"] == 1
        assert {row["sku"]: row["status"] for row in result["rows"]} == {
            "EGG-RED": "matched",
            "EGG-BLUE": "missing",
        }

    async def test_the_listing_options_travel_with_each_row(self, db):
        """So a rule can be written from the listing, before any order arrives."""
        listings = [
            _listing(
                1,
                "Dragon Egg",
                _inventory(("EGG-RED", {"Color": "Red"}), ("EGG-BLUE", {"Color": "Blue"})),
            )
        ]
        result = await catalog.reconcile(db, listings)
        assert result["rows"][0]["listing_options"] == [
            {"name": "Color", "values": ["Red", "Blue"]}
        ]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _patch_transport(monkeypatch, handler):
    from app.integrations import base as base_module

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_module.new_client(**kwargs)

    monkeypatch.setattr(etsy_api, "new_client", fake)


async def _connected(session, shop_id=4242):
    await credentials.save(
        session,
        PROVIDER_ETSY,
        {
            "keystring": "k",
            "shared_secret": "s",
            "access_token": "42.token",
            "refresh_token": "r",
            "expires_at": 9_999_999_999,
            "shop_id": shop_id,
        },
    )
    await session.commit()
    return await etsy_api.client_for(session)


class TestFetching:
    async def test_inline_inventory_costs_one_request(self, db, monkeypatch):
        client = await _connected(db)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "results": [
                        _listing(1, "Dragon Egg", _inventory(("EGG-RED", {"Color": "Red"})))
                    ]
                },
            )

        _patch_transport(monkeypatch, handler)
        listings, notes = await catalog.fetch_listings(client)
        assert len(listings) == 1
        assert len(calls) == 1
        assert notes == []

    async def test_missing_inventory_is_fetched_per_listing_and_said_so(
        self, db, monkeypatch
    ):
        """Etsy does not always honour includes=Inventory."""
        client = await _connected(db)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/inventory"):
                return httpx.Response(200, json=_inventory(("EGG-RED", {"Color": "Red"})))
            return httpx.Response(200, json={"results": [_listing(1, "Dragon Egg")]})

        _patch_transport(monkeypatch, handler)
        listings, notes = await catalog.fetch_listings(client)
        assert any(path.endswith("/inventory") for path in calls)
        assert etsy_api.listing_inventory_of(listings[0]) is not None
        # A comparison built from a second round of calls should say so.
        assert notes and "one listing at a time" in notes[0]

    async def test_the_per_listing_fallback_is_bounded(self, db, monkeypatch):
        """A large shop must not quietly spend a thousand requests."""
        client = await _connected(db)
        many = [_listing(i, f"Listing {i}") for i in range(catalog.MAX_INVENTORY_FETCHES + 5)]
        inventory_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/inventory"):
                inventory_calls["n"] += 1
                return httpx.Response(200, json=_inventory(("X", {})))
            return httpx.Response(200, json={"results": many})

        _patch_transport(monkeypatch, handler)
        _, notes = await catalog.fetch_listings(client)
        assert inventory_calls["n"] == catalog.MAX_INVENTORY_FETCHES
        assert any("Stopped after reading variations" in note for note in notes)

    async def test_no_shop_selected_is_refused_clearly(self, db):
        client = await _connected(db, shop_id=None)
        with pytest.raises(IntegrationError) as excinfo:
            await catalog.fetch_listings(client)
        assert "No Etsy shop selected" in str(excinfo.value)


class TestTheCatalogEndpoint:
    async def test_it_reports_the_comparison(self, signed_in, db, monkeypatch):
        await _connected(db)
        await _product(db, "EGG-RED", "Red Egg")
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _listing(
                            1,
                            "Dragon Egg",
                            _inventory(
                                ("EGG-RED", {"Color": "Red"}),
                                ("EGG-BLUE", {"Color": "Blue"}),
                            ),
                        )
                    ]
                },
            )

        _patch_transport(monkeypatch, handler)
        body = (await signed_in.get("/api/integrations/etsy/catalog")).json()
        assert body["error"] is None
        assert body["counts"]["matched"] == 1
        assert body["counts"]["missing"] == 1

    async def test_a_token_without_the_listings_scope_says_to_reconnect(
        self, signed_in, db, monkeypatch
    ):
        """Orders keep working, so this shows up as one screen failing."""
        await _connected(db)
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, json={"error": "insufficient scope: listings_r required"}
            )

        _patch_transport(monkeypatch, handler)
        response = await signed_in.get("/api/integrations/etsy/catalog")
        # 200 with an explanation: this screen exists to report what is wrong.
        assert response.status_code == 200
        body = response.json()
        assert body["needs_reconnect"] is True
        assert "Reconnect Etsy" in body["error"]

    async def test_another_failure_is_not_blamed_on_the_scope(
        self, signed_in, db, monkeypatch
    ):
        await _connected(db)
        await db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "etsy is having a day"})

        _patch_transport(monkeypatch, handler)
        body = (await signed_in.get("/api/integrations/etsy/catalog")).json()
        assert body["needs_reconnect"] is False
        assert "having a day" in body["error"]

    async def test_no_shop_selected_is_reported_not_raised(self, signed_in, db):
        await _connected(db, shop_id=None)
        await db.commit()
        body = (await signed_in.get("/api/integrations/etsy/catalog")).json()
        assert body["error"] == "No Etsy shop selected yet."
        assert body["rows"] == []


class TestTheScope:
    async def test_listings_access_is_requested_at_connect_time(self):
        url = etsy_api.authorize_url(
            keystring="k", redirect_uri="https://x/cb", state="s", code_challenge="c"
        )
        assert "listings_r" in url
        # And the scopes that were already there are still there.
        assert "transactions_r" in url
        assert "shops_r" in url

    async def test_a_scope_error_is_recognised(self):
        exc = AuthExpiredError(
            "etsy",
            "Authorisation was rejected",
            status_code=403,
            body='{"error":"insufficient scope"}',
        )
        assert etsy_api.wants_listing_scope(exc)

    async def test_an_unrelated_403_is_not(self):
        exc = AuthExpiredError(
            "etsy", "rejected", status_code=403, body='{"error":"API key not found"}'
        )
        assert not etsy_api.wants_listing_scope(exc)

    async def test_a_server_error_is_not(self):
        exc = IntegrationError("etsy", "boom", status_code=500, body="scope")
        assert not etsy_api.wants_listing_scope(exc)
