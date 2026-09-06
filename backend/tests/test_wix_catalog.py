"""Recognising a Wix catalogue as things PrintFlow already knows how to make.

A shop running both channels almost always built its Wix catalogue by importing
from Etsy, so every Wix item has a counterpart here under another name. These
tests are about the join being made correctly — and, just as much, about it
refusing to guess when it cannot be sure.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations import base as base_api
from app.integrations import wix
from app.models import EtsyProductLink, Product, WixProductLink
from app.services import intake, wix_catalog
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


def item(item_id: str, name: str, sku: str | None = None, **extra) -> dict:
    return {
        "wix_catalog_item_id": item_id,
        "name": name,
        "sku": sku,
        "visible": True,
        "variants": [],
        **extra,
    }


async def a_product(db, sku: str, name: str) -> Product:
    product = Product(sku=sku, name=name, fulfillment="printed")
    db.add(product)
    await db.flush()
    return product


# --------------------------------------------------------------------------
# Reading the catalogue off either Stores generation
# --------------------------------------------------------------------------


def _patch_transport(monkeypatch, handler):
    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_api.new_client(**kwargs)

    monkeypatch.setattr(wix, "new_client", fake)


def _client() -> wix.WixClient:
    return wix.WixClient({"api_key": "k" * 12, "site_id": "site-1"})


async def test_it_falls_back_to_the_older_stores_api(monkeypatch):
    """Wix Stores has two live generations and a site may have either.

    Picking one and being wrong on half the installs is not a choice worth
    making, so a 404 on the newer one is a reason to try the older, not a
    reason to give up.
    """
    tried = []

    def handler(request):
        tried.append(request.url.path)
        if request.url.path == "/stores/v3/products/search":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(
            200,
            json={
                "products": [{"id": "a", "name": "Widget", "sku": "W-1"}],
                "totalResults": 1,
            },
        )

    _patch_transport(monkeypatch, handler)
    items, version = await _client().iter_catalog()

    assert tried[0] == "/stores/v3/products/search"
    assert tried[1] == "/stores-reader/v1/products/query"
    assert version == "v1"
    assert items[0]["sku"] == "W-1"


async def test_a_rejected_key_is_not_mistaken_for_the_wrong_api_version(monkeypatch):
    """Only a 404 means "not this generation".

    Treating every failure as one would try both endpoints and then report the
    second one's error, burying "your key is wrong" behind a red herring.
    """
    tried = []

    def handler(request):
        tried.append(request.url.path)
        return httpx.Response(401, json={"message": "invalid API key"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(base_api.AuthExpiredError):
        await _client().iter_catalog()

    assert len(tried) == 1


async def test_neither_generation_answers_says_something_useful(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"message": "not found"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(base_api.IntegrationError) as caught:
        await _client().iter_catalog()

    said = str(caught.value)
    assert "Wix Stores" in said and "permission" in said


async def test_the_version_is_remembered_between_pages(monkeypatch):
    """A long catalogue walk should not re-probe the dead endpoint every page."""
    tried = []
    page = {"n": 0}

    def handler(request):
        tried.append(request.url.path)
        if request.url.path == "/stores/v3/products/search":
            return httpx.Response(404, json={})
        page["n"] += 1
        rows = [{"id": f"i{page['n']}", "name": "Thing", "sku": f"S-{page['n']}"}]
        return httpx.Response(200, json={"products": rows, "totalResults": 3})

    _patch_transport(monkeypatch, handler)
    items, _version = await _client().iter_catalog(max_pages=3)

    assert len(items) == 3
    # One probe of v3, then only v1 from there on.
    assert tried.count("/stores/v3/products/search") == 1


# --------------------------------------------------------------------------
# Recognising an item
# --------------------------------------------------------------------------


async def test_the_product_code_wins(db):
    product = await a_product(db, "WIDGET-1", "Widget")
    found = await wix_catalog.reconcile(db, [item("w1", "Anything at all", "widget-1")])

    row = found["items"][0]
    assert row["status"] == wix_catalog.BY_SKU
    assert row["product_id"] == product.id
    assert row["matched_on"] == "product code"


async def test_an_item_with_no_sku_is_found_through_its_etsy_listing(db):
    """The case the whole feature exists for.

    A shop that never filled in SKUs matches Etsy orders by link, and its Wix
    catalogue — imported from those same listings — carries the listing titles.
    PrintFlow stored those titles when the Etsy links were made, so the join is
    transitive: this Wix item is that Etsy listing, and that listing is this
    product.
    """
    product = await a_product(db, "GEN-1", "Internal name nobody typed on Wix")
    db.add(
        EtsyProductLink(
            product_id=product.id,
            etsy_listing_id=12345,
            listing_title="Hand-thrown mug",
        )
    )
    await db.flush()

    found = await wix_catalog.reconcile(db, [item("w1", "Hand-thrown mug")])
    row = found["items"][0]

    assert row["status"] == wix_catalog.BY_ETSY_TITLE
    assert row["product_id"] == product.id
    assert row["matched_on"] == "its Etsy listing's title"


async def test_the_product_name_is_the_last_resort(db):
    product = await a_product(db, "GEN-2", "Dragon egg")
    found = await wix_catalog.reconcile(db, [item("w1", "dragon egg")])
    row = found["items"][0]

    assert row["status"] == wix_catalog.BY_PRODUCT_NAME
    assert row["product_id"] == product.id


async def test_an_ambiguous_etsy_title_matches_nothing(db):
    """Two listings with one title pointing at different products.

    This is precisely where a confident guess would be wrong, so the title is
    dropped rather than one of the two being picked.
    """
    first = await a_product(db, "A-1", "First")
    second = await a_product(db, "A-2", "Second")
    db.add_all(
        [
            EtsyProductLink(product_id=first.id, etsy_listing_id=1, listing_title="Mug"),
            EtsyProductLink(product_id=second.id, etsy_listing_id=2, listing_title="Mug"),
        ]
    )
    await db.flush()

    found = await wix_catalog.reconcile(db, [item("w1", "Mug")])
    assert found["items"][0]["status"] == wix_catalog.MISSING
    assert found["items"][0]["product_id"] is None


async def test_nothing_recognised_is_reported_rather_than_hidden(db):
    """A Wix order for one of these will stall, so it has to be visible."""
    found = await wix_catalog.reconcile(db, [item("w1", "Never seen", "NOPE-1")])
    row = found["items"][0]

    assert row["status"] == wix_catalog.MISSING
    assert row["product_id"] is None
    assert found["proposed"] == 0


async def test_an_item_already_linked_is_not_proposed_again(db):
    product = await a_product(db, "WIDGET-1", "Widget")
    db.add(WixProductLink(product_id=product.id, wix_catalog_item_id="w1"))
    await db.flush()

    found = await wix_catalog.reconcile(db, [item("w1", "Widget", "WIDGET-1")])
    row = found["items"][0]

    assert row["status"] == wix_catalog.LINKED
    assert row["product_id"] == product.id
    assert found["proposed"] == 0


# --------------------------------------------------------------------------
# Making the links
# --------------------------------------------------------------------------


async def test_linking_creates_only_what_was_asked_for(db):
    await a_product(db, "A-1", "A")
    await a_product(db, "B-1", "B")
    items = [item("wa", "A", "A-1"), item("wb", "B", "B-1")]

    result = await wix_catalog.link_items(db, items, ["wa"])

    assert len(result["linked"]) == 1
    links = (await db.execute(select(WixProductLink))).scalars().all()
    assert [link.wix_catalog_item_id for link in links] == ["wa"]


async def test_linking_is_rematched_rather_than_trusted(db):
    """The proposal was computed minutes ago against a catalogue read then.

    An id arriving from a browser is a request to link *that item*, not a
    promise about what it matches. Re-matching here is what stops a product
    renamed in between producing a link nobody agreed to.
    """
    await a_product(db, "A-1", "A")
    result = await wix_catalog.link_items(db, [item("wz", "Unknown", "NOPE")], ["wz"])

    assert result["linked"] == []
    assert result["skipped"][0]["reason"] == "nothing matches it"


async def test_an_item_wix_no_longer_sells_is_skipped_not_invented(db):
    await a_product(db, "A-1", "A")
    result = await wix_catalog.link_items(db, [item("wa", "A", "A-1")], ["gone"])

    assert result["linked"] == []
    assert result["skipped"][0]["reason"] == "Wix no longer sells it"


async def test_linking_twice_does_not_duplicate(db):
    await a_product(db, "A-1", "A")
    items = [item("wa", "A", "A-1")]

    await wix_catalog.link_items(db, items, ["wa"])
    again = await wix_catalog.link_items(db, items, ["wa"])

    assert again["linked"] == []
    assert again["skipped"][0]["reason"] == "already linked"
    links = (await db.execute(select(WixProductLink))).scalars().all()
    assert len(links) == 1


async def test_a_new_link_clears_the_orders_waiting_on_it(db):
    """Linking is only worth doing because it fixes orders.

    An item usually sells before anybody notices it never matched, so the
    backlog is the point — not the link.
    """
    product = await a_product(db, "WIDGET-1", "Widget")
    payload = {
        "id": "order-1",
        "number": "500",
        "lineItems": [
            {
                "id": "line-1",
                "productName": {"original": "Widget"},
                "quantity": 1,
                "price": {"amount": "10.00"},
                "physicalProperties": {},
                "catalogReference": {"catalogItemId": "wa"},
            }
        ],
    }
    order, _ = await intake.ingest_wix_order(db, payload)
    line = (
        await db.execute(select(intake.OrderLine).where(intake.OrderLine.order_id == order.id))
    ).scalar_one()
    assert line.product_id is None

    await wix_catalog.link_items(db, [item("wa", "Widget", "WIDGET-1")], ["wa"])
    link = (await db.execute(select(WixProductLink))).scalar_one()
    fixed = await intake.apply_wix_link(db, link)

    assert fixed == 1
    await db.refresh(line)
    assert line.product_id == product.id


# --------------------------------------------------------------------------
# Staying inside the proxy's patience
# --------------------------------------------------------------------------


async def test_the_backlog_pass_stops_at_its_budget(db, monkeypatch):
    """Linking a whole catalogue must answer, not run until a proxy gives up.

    Every link re-resolves the orders waiting on it, and each of those asks
    QuickBooks about stock and Bambuddy about plates. Two hundred links is
    two hundred rounds of that, which is minutes — and Cloudflare stops
    waiting at 100s and serves its own 502 page.
    """
    from app.routers import integrations_router as router

    product = await a_product(db, "A-1", "A")
    made = []
    for n in range(5):
        db.add(
            WixProductLink(product_id=product.id, wix_catalog_item_id=f"w{n}")
        )
        made.append({"wix_catalog_item_id": f"w{n}"})
    await db.commit()

    # Each link "costs" more than the whole budget, so only the first runs.
    calls = []

    async def slow(session, link):
        calls.append(link.wix_catalog_item_id)
        clock["now"] += router.BACKLOG_BUDGET_SECONDS + 1
        return 1

    clock = {"now": 0.0}
    monkeypatch.setattr(router.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(router.intake, "apply_wix_link", slow)

    fixed, remaining = await router._clear_wix_backlog(db, made)

    assert len(calls) == 1
    assert fixed == 1
    # And it says what it did not reach rather than pretending it finished.
    assert remaining == 4


async def test_one_unresolvable_order_does_not_cost_the_pass(db, monkeypatch):
    """The links are already committed; a bad order must not undo the rest."""
    from app.routers import integrations_router as router

    product = await a_product(db, "A-1", "A")
    for n in range(3):
        db.add(WixProductLink(product_id=product.id, wix_catalog_item_id=f"w{n}"))
    await db.commit()

    seen = []

    async def sometimes(session, link):
        seen.append(link.wix_catalog_item_id)
        if link.wix_catalog_item_id == "w1":
            raise RuntimeError("this one will not resolve")
        return 1

    monkeypatch.setattr(router.intake, "apply_wix_link", sometimes)

    fixed, remaining = await router._clear_wix_backlog(
        db, [{"wix_catalog_item_id": f"w{n}"} for n in range(3)]
    )

    assert seen == ["w0", "w1", "w2"]
    assert fixed == 2
    assert remaining == 0
    # The links themselves survive, because they were never in doubt.
    links = (await db.execute(select(WixProductLink))).scalars().all()
    assert len(links) == 3
