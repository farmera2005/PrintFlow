"""Wix: reading what the site sends, and taking it through the same pipeline.

The claim this file is here to defend is that an order from Wix is an order.
Not "an order with a Wix flavour" — the same rows, the same matching, the same
board, the same invoice. So most of these tests are the Etsy tests asked again
of the other channel, and the ones that are not are about the two channels
staying out of each other's way.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.integrations import base as base_api
from app.integrations import wix
from app.integrations.base import IntegrationError
from app.models import (
    LINE_UNMATCHED,
    SOURCE_ETSY,
    SOURCE_WIX,
    Order,
    OrderLine,
    Product,
    WixProductLink,
)
from app.services import intake

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# A realistic payload
# --------------------------------------------------------------------------


def an_order(**overrides) -> dict:
    """One order as Wix's search endpoint returns it.

    Kept whole rather than trimmed to what each test reads: the shapes that
    caused trouble are the nested ones — money as an object, options in two
    different places — and a payload cut down to a flat dictionary would stop
    testing them.
    """
    payload = {
        "id": "0f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
        "number": "10042",
        "createdDate": "2026-08-01T12:30:00.000Z",
        "currency": "USD",
        "status": "APPROVED",
        "archived": False,
        "buyerInfo": {"email": "dana@example.invalid"},
        "recipientInfo": {
            "contactDetails": {
                "firstName": "Dana",
                "lastName": "Buyer",
                "phone": "555-0100",
            },
            "address": {
                "addressLine": "12 Kiln Lane",
                "city": "Kilnford",
                "subdivision": "OH",
                "postalCode": "45011",
                "country": "US",
            },
        },
        "priceSummary": {
            "total": {"amount": "42.19", "formattedAmount": "$42.19"},
            "subtotal": {"amount": "36.00"},
            "shipping": {"amount": "4.50"},
            "tax": {"amount": "2.69"},
            "discount": {"amount": "1.00"},
        },
        "lineItems": [
            {
                "id": "line-1",
                "productName": {"original": "Dragon egg"},
                "quantity": 2,
                "price": {"amount": "18.00"},
                "physicalProperties": {"sku": "EGG-1"},
                "catalogReference": {
                    "catalogItemId": "cat-egg",
                    "options": {
                        "variantId": "var-red",
                        "options": {"Color": "Red"},
                    },
                },
                "descriptionLines": [
                    {
                        "name": {"original": "Engraving"},
                        "plainText": {"original": "For Sam"},
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


async def test_an_order_arrives_in_printflows_words():
    parsed = wix.parse_order(an_order())

    assert parsed["wix_order_id"] == "0f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
    assert parsed["number"] == "10042"
    assert parsed["placed_at"].year == 2026
    assert parsed["placed_at"].tzinfo is not None
    assert parsed["buyer_name"] == "Dana Buyer"
    assert parsed["status"] == "APPROVED"


async def test_money_stays_exact():
    """Every figure a Decimal, never a float.

    This is the whole reason the amounts are strings in Wix's JSON and stay
    strings until Decimal takes them: 18.10 through a float is
    18.099999999999998, and that ends up in somebody's books.
    """
    parsed = wix.parse_order(an_order())

    assert parsed["currency"] == "USD"
    assert parsed["revenue"] == Decimal("42.19")
    assert parsed["items_total"] == Decimal("36.00")
    assert parsed["shipping_total"] == Decimal("4.50")
    assert parsed["tax_total"] == Decimal("2.69")
    assert parsed["discount_total"] == Decimal("1.00")
    assert all(isinstance(parsed[f], Decimal) for f in ("revenue", "tax_total"))


async def test_a_price_that_is_not_a_number_is_absent_not_zero():
    """Nothing is not zero. A missing total must not read as a free order."""
    payload = an_order()
    payload["priceSummary"] = {"total": {"amount": ""}, "subtotal": None}
    parsed = wix.parse_order(payload)

    assert parsed["revenue"] is None
    assert parsed["items_total"] is None


async def test_the_line_carries_both_ids_and_the_unit_price():
    line = wix.parse_order(an_order())["lines"][0]

    assert line["wix_line_item_id"] == "line-1"
    assert line["wix_catalog_item_id"] == "cat-egg"
    assert line["wix_variant_id"] == "var-red"
    assert line["sku"] == "EGG-1"
    assert line["title"] == "Dragon egg"
    assert line["quantity"] == 2
    # Per unit, not per line — 36.00 is what two of them came to.
    assert line["unit_price"] == Decimal("18.00")


async def test_options_are_read_from_both_places_wix_puts_them():
    """A Stores variant and a custom text field, on one line.

    Wix uses `catalogReference.options.options` for a catalogue variant and
    `descriptionLines` for typed-in text, and a shop can sell an item that has
    both. Either can be the one that changes what gets made, so both are read.
    """
    options = wix.parse_order(an_order())["lines"][0]["options"]

    assert {"name": "Color", "value": "Red"} in options
    assert {"name": "Engraving", "value": "For Sam"} in options


async def test_the_recipient_is_preferred_over_the_buyer():
    """A gift goes to somebody other than whoever paid for it.

    The name that matters to a shop packing a box is the one going on the
    label, so the recipient wins.
    """
    payload = an_order()
    payload["billingInfo"] = {
        "contactDetails": {"firstName": "Robin", "lastName": "Payer"}
    }
    parsed = wix.parse_order(payload)

    assert parsed["buyer_name"] == "Dana Buyer"
    assert parsed["ship_to"]["city"] == "Kilnford"
    assert parsed["ship_to"]["first_line"] == "12 Kiln Lane"
    assert parsed["ship_to"]["email"] == "dana@example.invalid"


async def test_an_order_with_no_name_anywhere_falls_back_to_the_email():
    payload = an_order()
    payload.pop("recipientInfo")
    parsed = wix.parse_order(payload)

    assert parsed["buyer_name"] == "dana@example.invalid"


async def test_a_refusal_says_what_to_do_about_it():
    """403 is the one worth explaining: the key is fine, its permissions are not.

    The number on its own is not an answer — a 401 and a 403 are different
    mistakes with different fixes, and neither is obvious from the status code
    to the person who has to go and fix it.
    """
    plain = IntegrationError("wix", "Wix refused the request", status_code=403)
    explained = str(wix._explain(plain))

    assert "permissions" in explained.lower()
    assert "Wix refused the request" in explained  # what Wix said is kept

    # A status code with nothing useful to add is passed through untouched
    # rather than dressed up in a guess.
    unknown = IntegrationError("wix", "Wix had a bad day", status_code=500)
    assert wix._explain(unknown) is unknown


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------


async def a_product(db, sku: str = "EGG-1") -> Product:
    product = Product(sku=sku, name="Dragon egg", fulfillment="printed")
    db.add(product)
    await db.flush()
    return product


async def test_a_wix_order_becomes_an_order(db):
    await a_product(db)
    order, created = await intake.ingest_wix_order(db, an_order())

    assert created is True
    assert order.source == SOURCE_WIX
    assert order.external_id == "0f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
    # Wix's own number, which is what the shop and the buyer both call it —
    # not the GUID, which nobody says out loud.
    assert order.order_number == "10042"
    assert order.etsy_receipt_id is None
    assert order.buyer_name == "Dana Buyer"
    assert order.revenue == Decimal("42.19")
    assert order.ship_to["city"] == "Kilnford"


async def test_the_line_matches_on_its_sku_like_any_other(db):
    product = await a_product(db)
    order, _ = await intake.ingest_wix_order(db, an_order())

    line = (
        await db.execute(
            select(OrderLine).where(OrderLine.order_id == order.id)
        )
    ).scalar_one()
    assert line.product_id == product.id
    assert line.state != LINE_UNMATCHED
    assert line.quantity == 2
    assert line.wix_catalog_item_id == "cat-egg"
    assert line.wix_variant_id == "var-red"
    # The options came down with it, so the operator sees "Red" without
    # opening Wix.
    assert {"name": "Color", "value": "Red"} in line.variations


async def test_importing_the_same_order_twice_does_not_double_it(db):
    """The poll re-reads a window of orders every time it runs.

    Idempotency is not an optimisation here — without it a shop's board fills
    with the same order once every five minutes.
    """
    await a_product(db)
    first, created_first = await intake.ingest_wix_order(db, an_order())
    second, created_second = await intake.ingest_wix_order(db, an_order())

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    lines = (
        (await db.execute(select(OrderLine).where(OrderLine.order_id == first.id)))
        .scalars()
        .all()
    )
    assert len(lines) == 1


async def test_a_correction_made_on_wix_reaches_the_order(db):
    """An address fixed after the fact, on the next poll — without new lines."""
    await a_product(db)
    order, _ = await intake.ingest_wix_order(db, an_order())

    corrected = an_order()
    corrected["recipientInfo"]["address"]["city"] = "Farhaven"
    corrected["priceSummary"]["discount"] = {"amount": "5.00"}
    await intake.ingest_wix_order(db, corrected)

    await db.refresh(order)
    assert order.ship_to["city"] == "Farhaven"
    assert order.discount_total == Decimal("5.00")


async def test_an_order_with_no_id_is_refused(db):
    payload = an_order()
    payload.pop("id")
    with pytest.raises(ValueError):
        await intake.ingest_wix_order(db, payload)


async def test_one_unreadable_order_does_not_stop_the_others(db):
    """A poll that gives up on the first bad row imports nothing all day."""
    await a_product(db)
    good = an_order()
    bad = an_order()
    bad.pop("id")
    other = an_order(id="second-order", number="10043")

    stats = await intake.ingest_wix_orders(db, [bad, good, other])

    assert stats == {"seen": 3, "created": 2, "skipped": 0, "errors": 1}


# --------------------------------------------------------------------------
# Matching by hand, and remembering it
# --------------------------------------------------------------------------


async def a_wix_order_without_skus(db) -> Order:
    payload = an_order()
    payload["lineItems"][0]["physicalProperties"] = {}
    order, _ = await intake.ingest_wix_order(db, payload)
    return order


async def test_a_line_with_no_sku_waits_to_be_matched(db):
    await a_product(db)
    order = await a_wix_order_without_skus(db)

    line = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == order.id))
    ).scalar_one()
    assert line.state == LINE_UNMATCHED
    assert line.product_id is None


async def test_matching_one_by_hand_is_remembered_for_the_next_one(db):
    """The point of the link: fix it once, and the catalogue item is known.

    An item usually sells more than once before anybody notices it never
    matched, so remembering it must clear the backlog too — not only the line
    the operator happened to be looking at.
    """
    product = await a_product(db)
    first = await a_wix_order_without_skus(db)
    second_payload = an_order(id="second-order", number="10043")
    second_payload["lineItems"][0]["physicalProperties"] = {}
    second, _ = await intake.ingest_wix_order(db, second_payload)

    line = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == first.id))
    ).scalar_one()
    await intake.relink_line(db, line, product)
    link = await intake.remember_channel_link(
        db, line, product, intake.LINK_SCOPE_LISTING
    )

    assert isinstance(link, WixProductLink)
    assert link.wix_catalog_item_id == "cat-egg"
    assert link.wix_variant_id is None

    also_fixed = await intake.apply_channel_link(db, link)
    assert also_fixed == 1

    other = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == second.id))
    ).scalar_one()
    assert other.product_id == product.id


async def test_a_variant_link_only_covers_that_variant(db):
    """"This colour is that product" is a narrower claim than "this item is"."""
    product = await a_product(db)
    order = await a_wix_order_without_skus(db)
    line = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == order.id))
    ).scalar_one()

    link = await intake.remember_channel_link(
        db, line, product, intake.LINK_SCOPE_VARIANT
    )
    assert link.wix_variant_id == "var-red"

    found, how = await intake.match_by_channel(db, line)
    assert found is not None and found.id == product.id
    assert how == "wix_variant"

    # A different colour of the same item is not covered by it.
    line.wix_variant_id = "var-blue"
    await db.flush()
    missed, _ = await intake.match_by_channel(db, line)
    assert missed is None


async def test_relinking_corrects_a_wrong_link_rather_than_refusing(db):
    product = await a_product(db)
    other = await a_product(db, sku="EGG-2")
    order = await a_wix_order_without_skus(db)
    line = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == order.id))
    ).scalar_one()

    await intake.remember_channel_link(db, line, product, intake.LINK_SCOPE_LISTING)
    await intake.remember_channel_link(db, line, other, intake.LINK_SCOPE_LISTING)

    links = (await db.execute(select(WixProductLink))).scalars().all()
    assert len(links) == 1
    assert links[0].product_id == other.id


# --------------------------------------------------------------------------
# The two channels stay out of each other's way
# --------------------------------------------------------------------------


async def test_an_etsy_line_is_matched_by_etsy_ids_and_a_wix_line_by_wix_ids(db):
    """A line has one channel's identity, never both, so this is a dispatch.

    Worth pinning: the alternative — searching both tables for every line —
    is how a Wix catalogue item whose GUID happens to parse as a number gets
    matched against an Etsy listing.
    """
    product = await a_product(db)
    order = await a_wix_order_without_skus(db)
    line = (
        await db.execute(select(OrderLine).where(OrderLine.order_id == order.id))
    ).scalar_one()
    # Both sets of ids on one line, which cannot happen in practice — asked
    # here only to prove which one is consulted.
    line.etsy_listing_id = 12345
    await db.flush()
    await intake.remember_channel_link(db, line, product, intake.LINK_SCOPE_LISTING)

    found, how = await intake.match_by_channel(db, line)
    assert how == "wix_item"
    assert found.id == product.id


async def test_an_etsy_order_and_a_wix_order_can_share_a_number(db):
    """Two shop windows number their own orders, and both start at 1.

    Uniqueness is per channel for exactly this reason: a collision between
    Etsy receipt 10042 and Wix order 10042 is a matter of time, not a
    hypothetical.
    """
    await a_product(db)
    wix_order, _ = await intake.ingest_wix_order(db, an_order())
    etsy_order = Order(
        source=SOURCE_ETSY,
        etsy_receipt_id=10042,
        order_number="10042",
        buyer_name="Robin Payer",
    )
    db.add(etsy_order)
    await db.flush()

    both = (
        (await db.execute(select(Order).order_by(Order.source))).scalars().all()
    )
    assert [order.source for order in both] == [SOURCE_ETSY, SOURCE_WIX]
    assert wix_order.order_number == etsy_order.order_number


async def test_an_order_taken_before_wix_existed_is_still_an_etsy_order(db):
    """The column has a default, so nothing that predates the channel changes."""
    order = Order(etsy_receipt_id=777, order_number="777")
    db.add(order)
    await db.flush()

    assert order.source == SOURCE_ETSY
    assert order.external_id is None


# --------------------------------------------------------------------------
# Answering before the proxy gives up
# --------------------------------------------------------------------------
#
# PrintFlow sits behind a reverse proxy. Cloudflare stops waiting at 100s and
# serves its own 502 page — which names PrintFlow's hostname rather than the
# service that was actually unreachable, so the operator is told PrintFlow is
# broken and goes looking in the wrong place entirely.
#
# "Save & validate" is the endpoint most likely to hit it, because it is the
# one that talks to a third party while somebody watches a spinner. These pin
# the two properties that keep it from happening: it tries once, and it is
# capped whatever happens out there.


def _patch_transport(monkeypatch, handler):
    """Point the Wix client's HTTP client at a canned handler."""

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_api.new_client(**kwargs)

    monkeypatch.setattr(wix, "new_client", fake)


def _client() -> wix.WixClient:
    return wix.WixClient({"api_key": "k" * 12, "site_id": "site-1"})


async def test_validate_asks_once_rather_than_four_times(monkeypatch):
    """The retry budget is what pushed this past the proxy's patience.

    Three retries at a 30s read timeout, plus backoff, is over two minutes —
    so the proxy answered first and the operator never saw the real error. A
    credential check has nothing to gain from retrying anyway: a key that is
    wrong is still wrong on the fourth attempt.
    """
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        return httpx.Response(503, json={"message": "try later"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(IntegrationError):
        await _client().validate()

    assert len(attempts) == 1


async def test_the_poll_still_retries(monkeypatch):
    """Nobody is watching a background poll, so a blip should not cost a cycle."""
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        # Fails once, then succeeds: enough to prove the retry happened,
        # without paying for the whole backoff ladder in test time.
        if len(attempts) < 2:
            return httpx.Response(503, json={"message": "try later"})
        return httpx.Response(200, json={"orders": [], "metadata": {"cursors": {}}})

    _patch_transport(monkeypatch, handler)
    found = await _client().search_orders()

    assert found == {"orders": [], "metadata": {"cursors": {}}}
    assert len(attempts) == 2


async def test_validate_is_capped_even_if_wix_never_answers(monkeypatch):
    """The backstop. Whatever happens out there, the endpoint answers.

    A request that hangs forever is the case the retry cap alone does not
    cover, and it is the one that produced the proxy's 502.
    """
    async def handler(request):
        await asyncio.sleep(3600)
        raise AssertionError("should never get here")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(base_api.DeadlineExceeded) as caught:
        async with base_api.deadline(
            "wix", "Checking the Wix API key", seconds=0.2, hint="check the network"
        ):
            await _client().validate()

    said = str(caught.value)
    assert "Checking the Wix API key" in said
    # The hint is the point: the default one talks about an address and a port,
    # which is nonsense for an API whose address the operator never typed.
    assert "check the network" in said
    assert "address and port" not in said


async def test_adding_a_hint_keeps_the_kind_of_failure_it_was():
    """A 401 is credentials being refused, and must stay that.

    `_explain` rebuilds the exception to add its hint, and rebuilding it as a
    plain IntegrationError quietly threw away the subclass — so the route
    answered 502 ("upstream is broken") to a key the operator had simply
    mistyped, and the app-wide "reconnect it in Settings" handler stopped
    seeing Wix at all.
    """
    rejected = base_api.AuthExpiredError(
        "wix", "Authorisation was rejected", status_code=401
    )
    explained = wix._explain(rejected)

    assert isinstance(explained, base_api.AuthExpiredError)
    assert "copied whole" in str(explained)

    # And a plain failure stays plain rather than being promoted.
    plain = IntegrationError("wix", "Wix refused the request", status_code=404)
    assert type(wix._explain(plain)) is IntegrationError
