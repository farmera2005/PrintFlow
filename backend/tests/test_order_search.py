"""Finding an order from whatever the person happens to remember.

The old search read two fields, the order number and the buyer's name, which
is fine until somebody is holding a parcel with a tracking number on it, or
has been asked whether the shop ever made a cable bracket for a trade
customer, or wrote a note last Tuesday and remembers three words of it. In
every one of those the order exists, the search says nothing matched, and
somebody scrolls two hundred rows instead.

What is worth testing is not that LIKE works. It is the three claims this
search makes that a naive one does not: every word has to match something
but not the same something, a word a person types is a word and not a
wildcard, and a result whose match is invisible says why it is there.
"""

from __future__ import annotations

import pytest

from app.models import Order, OrderLine, Product
from app.services import order_search

pytestmark = pytest.mark.asyncio


async def _product(db, sku="BRK-14", name="Cable bracket") -> Product:
    product = Product(sku=sku, name=name, fulfillment="printed")
    db.add(product)
    await db.flush()
    return product


async def _order(db, number="9001", buyer="Dana Buyer", **fields) -> Order:
    order = Order(
        source="etsy",
        etsy_receipt_id=int(number),
        order_number=number,
        buyer_name=buyer,
        **fields,
    )
    db.add(order)
    await db.flush()
    return order


async def _line(db, order, product=None, *, title=None, sku_raw=None, **fields):
    line = OrderLine(
        order_id=order.id,
        product_id=product.id if product else None,
        sku_raw=sku_raw if sku_raw is not None else (product.sku if product else "?"),
        title=title if title is not None else (product.name if product else "Unknown"),
        quantity=1,
        qty_to_print=1,
        state="new",
        **fields,
    )
    db.add(line)
    await db.flush()
    return line


async def _found(signed_in, query: str) -> list[str]:
    # Passed as a parameter rather than pasted into the path, so a query
    # containing a `%` is a test of the search and not of URL encoding.
    body = (await signed_in.get("/api/orders", params={"q": query})).json()
    return [order["order_number"] for order in body["orders"]]


# --------------------------------------------------------------------------
# Reading the query
# --------------------------------------------------------------------------


class TestTheWords:
    async def test_a_query_is_its_words(self):
        assert order_search.terms("  dana   bracket  ") == ["dana", "bracket"]

    async def test_the_same_word_twice_is_one_word(self):
        """Typing it again is a slip, not an instruction to search harder."""
        assert order_search.terms("dana Dana dana") == ["dana"]

    async def test_a_query_longer_than_anybody_means_is_trimmed(self):
        """Each word is another OR across six fields and an EXISTS over the
        lines. Nobody types eight words into a search box with a plan."""
        words = order_search.terms("a b c d e f g h i")
        assert words == ["a", "b", "c", "d", "e", "f"]
        assert order_search.describe(0, "a b c d e f g h i")["trimmed"] is True

    async def test_nothing_asked_is_every_order(self):
        assert order_search.conditions("") == []
        assert order_search.conditions("   ") == []

    async def test_a_wildcard_is_a_character_somebody_typed(self):
        """`%` and `_` are wildcards to SQL and ordinary characters to the
        person typing them. A search for 50% has to mean fifty per cent."""
        assert order_search._needle("50%") == "%50\\%%"
        assert order_search._needle("a_b") == "%a\\_b%"
        assert order_search._needle("back\\slash") == "%back\\\\slash%"


# --------------------------------------------------------------------------
# What it looks in
# --------------------------------------------------------------------------


class TestWhereItLooks:
    async def test_the_order_number(self, db, signed_in):
        await _order(db, "9001")
        await _order(db, "9002")
        await db.commit()
        assert await _found(signed_in, "9001") == ["9001"]

    async def test_part_of_the_order_number(self, db, signed_in):
        """Nobody reads the whole number off a parcel."""
        await _order(db, "7301")
        await db.commit()
        assert await _found(signed_in, "301") == ["7301"]

    async def test_the_buyer(self, db, signed_in):
        await _order(db, "9001", buyer="Dana Buyer")
        await _order(db, "9002", buyer="Sam Post")
        await db.commit()
        assert await _found(signed_in, "dana") == ["9001"]

    async def test_the_tracking_number(self, db, signed_in):
        """Somebody is holding the parcel. This is the thing printed on it."""
        await _order(db, "9001", tracking_number="1Z999AA10123456784")
        await _order(db, "9002")
        await db.commit()
        assert await _found(signed_in, "10123456784") == ["9001"]

    async def test_the_invoice_number(self, db, signed_in):
        """What a person searches for in QuickBooks, asked here instead."""
        await _order(db, "9001", qbo_invoice_doc_number="INV-2026-118")
        await _order(db, "9002")
        await db.commit()
        assert await _found(signed_in, "INV-2026-118") == ["9001"]

    async def test_the_note(self, db, signed_in):
        """Written last Tuesday, three words of it remembered."""
        await _order(db, "9001", notes="Buyer asked for it before the wedding.")
        await _order(db, "9002", notes="Reprint — first one warped.")
        await db.commit()
        assert await _found(signed_in, "wedding") == ["9001"]

    async def test_a_line_by_its_listing_name(self, db, signed_in):
        """"Did we ever make the cable bracket for that trade customer?" is
        a question about an item, asked of a list of orders."""
        part = await _product(db)
        wanted = await _order(db, "9001")
        await _line(db, wanted, part)
        other = await _order(db, "9002")
        await _line(db, other, await _product(db, sku="KNOB-S", name="Small knob"))
        await db.commit()
        assert await _found(signed_in, "bracket") == ["9001"]

    async def test_a_line_by_its_code(self, db, signed_in):
        part = await _product(db)
        order = await _order(db, "9001")
        await _line(db, order, part)
        await db.commit()
        assert await _found(signed_in, "BRK-14") == ["9001"]

    async def test_a_line_nothing_matched_is_still_searchable(self, db, signed_in):
        """It has no product, so only what the channel called it exists —
        and that is exactly the order somebody is hunting for."""
        order = await _order(db, "9001")
        await _line(db, order, None, title="Mystery widget", sku_raw="MYSTERY-9")
        await db.commit()
        assert await _found(signed_in, "mystery widget") == ["9001"]

    async def test_a_percent_sign_is_searched_for_not_obeyed(self, db, signed_in):
        """The pattern builder escapes it; this is the proof that the
        escape reaches the database, which is where it matters."""
        await _order(db, "9001", notes="Offered 50% off.")
        await _order(db, "9002", notes="No discount.")
        await db.commit()
        assert await _found(signed_in, "50%") == ["9001"]
        # And a bare `%` is a search for a percent sign: it finds the order
        # that has one and, far more to the point, not the one that does not.
        assert await _found(signed_in, "%") == ["9001"]

    async def test_an_underscore_is_a_character_too(self, db, signed_in):
        await _order(db, "9001", notes="File is part_a.3mf")
        await _order(db, "9002", notes="File is partXa.3mf")
        await db.commit()
        assert await _found(signed_in, "part_a") == ["9001"]

    async def test_an_order_with_four_matching_lines_is_one_row(self, db, signed_in):
        """A join would quietly turn the page limit into a limit on
        something other than orders."""
        part = await _product(db)
        order = await _order(db, "9001")
        for _ in range(4):
            await _line(db, order, part)
        await db.commit()
        assert await _found(signed_in, "bracket") == ["9001"]


# --------------------------------------------------------------------------
# How the words work together
# --------------------------------------------------------------------------


class TestSeveralWords:
    async def test_every_word_must_match_but_not_the_same_field(self, db, signed_in):
        """The claim that makes a two-word query worth typing: "dana
        bracket" is two facts about one order, not a string inside it."""
        part = await _product(db)
        hit = await _order(db, "9001", buyer="Dana Buyer")
        await _line(db, hit, part)

        wrong_item = await _order(db, "9002", buyer="Dana Buyer")
        await _line(db, wrong_item, await _product(db, sku="KNOB-S", name="Small knob"))

        wrong_buyer = await _order(db, "9003", buyer="Sam Post")
        await _line(db, wrong_buyer, part)
        await db.commit()

        assert await _found(signed_in, "dana bracket") == ["9001"]

    async def test_adding_a_word_always_narrows(self, db, signed_in):
        part = await _product(db)
        for number in ("9001", "9002"):
            order = await _order(db, number, buyer="Dana Buyer")
            await _line(db, order, part)
        await db.commit()

        assert len(await _found(signed_in, "dana")) == 2
        assert len(await _found(signed_in, "dana bracket")) == 2
        assert await _found(signed_in, "dana bracket 9002") == ["9002"]

    async def test_a_word_matching_nothing_empties_the_result(self, db, signed_in):
        order = await _order(db, "9001", buyer="Dana Buyer")
        await _line(db, order, await _product(db))
        await db.commit()
        assert await _found(signed_in, "dana unicorn") == []


# --------------------------------------------------------------------------
# Saying why
# --------------------------------------------------------------------------


class TestWhyItMatched:
    async def test_a_match_on_a_note_says_so(self, db, signed_in):
        """Otherwise the row looks like it has nothing to do with the
        search, which reads as a bug rather than as a hit."""
        await _order(db, "9001", notes="Buyer asked for it before the wedding.")
        await db.commit()
        body = (await signed_in.get("/api/orders?q=wedding")).json()
        assert body["orders"][0]["matched_on"] == ["the note"]

    async def test_a_match_on_an_item_names_the_item(self, db, signed_in):
        part = await _product(db)
        order = await _order(db, "9001")
        await _line(db, order, part)
        await db.commit()
        body = (await signed_in.get("/api/orders?q=brk-14")).json()
        assert body["orders"][0]["matched_on"] == ["item “Cable bracket”"]

    async def test_a_match_on_the_buyer_says_nothing(self, db, signed_in):
        """The buyer's name is drawn on the row. Saying the match was on the
        buyer's name, next to the buyer's name, is noise."""
        await _order(db, "9001", buyer="Dana Buyer")
        await db.commit()
        body = (await signed_in.get("/api/orders?q=dana")).json()
        assert "matched_on" not in body["orders"][0]

    async def test_a_match_on_the_number_says_nothing(self, db, signed_in):
        await _order(db, "9001")
        await db.commit()
        body = (await signed_in.get("/api/orders?q=9001")).json()
        assert "matched_on" not in body["orders"][0]

    async def test_three_lines_matching_one_word_is_one_reason(self, db, signed_in):
        part = await _product(db)
        order = await _order(db, "9001")
        for _ in range(3):
            await _line(db, order, part)
        await db.commit()
        body = (await signed_in.get("/api/orders?q=bracket")).json()
        assert body["orders"][0]["matched_on"] == ["item “Cable bracket”"]

    async def test_two_reasons_are_both_given(self, db, signed_in):
        part = await _product(db)
        order = await _order(db, "9001", notes="Chase the wedding one.")
        await _line(db, order, part)
        await db.commit()
        body = (await signed_in.get("/api/orders?q=wedding bracket")).json()
        assert body["orders"][0]["matched_on"] == [
            "the note",
            "item “Cable bracket”",
        ]

    async def test_nothing_asked_explains_nothing(self, db, signed_in):
        await _order(db, "9001", notes="A note.")
        await db.commit()
        body = (await signed_in.get("/api/orders")).json()
        assert "matched_on" not in body["orders"][0]


# --------------------------------------------------------------------------
# Alongside everything else the list does
# --------------------------------------------------------------------------


class TestWithTheRestOfTheScreen:
    async def test_search_and_the_column_filter_both_apply(self, db, signed_in):
        await _order(db, "9001", buyer="Dana Buyer", status="new")
        await _order(db, "9002", buyer="Dana Buyer", status="cancelled")
        await db.commit()

        body = (
            await signed_in.get("/api/orders?q=dana&status_filter=cancelled")
        ).json()
        assert [order["order_number"] for order in body["orders"]] == ["9002"]

    async def test_a_cancelled_order_is_findable(self, db, signed_in):
        """The board never draws one. This is the only way back to it."""
        await _order(db, "9001", buyer="Dana Buyer", status="cancelled")
        await db.commit()
        assert await _found(signed_in, "dana") == ["9001"]

    async def test_the_counts_are_of_the_shop_not_of_the_search(self, db, signed_in):
        """They label the column filters, which have to go on saying what is
        in a column while a search is narrowing what is shown."""
        await _order(db, "9001", buyer="Dana Buyer")
        await _order(db, "9002", buyer="Sam Post")
        await db.commit()

        body = (await signed_in.get("/api/orders?q=dana")).json()
        assert len(body["orders"]) == 1
        assert body["counts"]["new"] == 2
        assert body["total"] == 2
        # And the search says what it actually did, so the screen can show
        # "1 of 2" rather than the wrong one of those numbers.
        assert body["search"] == {"terms": ["dana"], "results": 1, "trimmed": False}

    async def test_it_takes_a_signed_in_user(self, db, client):
        assert (await client.get("/api/orders?q=dana")).status_code == 401
