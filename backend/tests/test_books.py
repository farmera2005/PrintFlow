"""What the order pipeline writes into QuickBooks.

Two writes that reach somebody's real accounting system: stock leaving when a
line is printed, and an invoice when the order is billed. The stakes are the
same as the manufacturing suite's — a wrong number here is a wrong number on a
tax return — and the thing most worth pinning is not that the writes happen but
that they happen *once* and that they do not overlap.

The overlap is the subtle one. An invoice line naming an inventory item takes
that unit out of stock all by itself, and the printed line has already done
exactly that — so invoicing hands the job over, taking back the printed
removal for the lines the invoice now covers. Several tests exist only to pin
that hand-over, in both directions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.integrations import qbo as qbo_api
from app.models import (
    JOB_DONE,
    JOB_PRINTING,
    LINE_CANCELLED,
    LINE_PRINTED,
    PROVIDER_QBO,
    Order,
    OrderLine,
    PrintJob,
    Product,
    ProductOptionItem,
    ProductVariation,
)
from app.services import books, credentials, settings_store
from app.services.books import (
    BooksError,
    build_invoice,
    build_removal,
    customer_payload,
    invoice_item_for,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


async def _product(session, sku="BIN", name="Storage bin", qbo_id="500", fulfillment="printed"):
    product = Product(
        sku=sku, name=name, fulfillment=fulfillment, qbo_item_id=qbo_id, qbo_item_name=name
    )
    session.add(product)
    await session.flush()
    await session.refresh(product)
    return product


async def _order(session, *, number="9001", buyer="Dana Buyer", transactions=None, **fields):
    """An order with a receipt behind it, because the invoice reads the receipt.

    Prices live on Etsy's transactions rather than on PrintFlow's lines, so an
    order with no `raw` is an order with nothing to invoice — which is itself
    one of the cases below.
    """
    order = Order(
        etsy_receipt_id=int(number),
        order_number=number,
        buyer_name=buyer,
        placed_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        currency="USD",
        ship_to={
            "name": buyer,
            "first_line": "12 Kiln Row",
            "city": "Toledo",
            "state": "OH",
            "zip": "43601",
            "country": "US",
        },
        raw={"transactions": transactions} if transactions is not None else None,
        **fields,
    )
    session.add(order)
    await session.flush()
    await session.refresh(order)
    return order


async def _line(session, order, product, *, quantity=2, transaction_id=1, **fields):
    line = OrderLine(
        order_id=order.id,
        product_id=product.id if product else None,
        etsy_transaction_id=transaction_id,
        sku_raw=product.sku if product else "GHOST",
        title=product.name if product else "Unknown",
        quantity=quantity,
        **{"qty_to_print": quantity, **fields},
    )
    session.add(line)
    await session.flush()
    await session.refresh(line)
    return line


async def _reloaded(session, line):
    """The line as a request would load it: product, its mappings, variation.

    Setting `variations` on an object already in the identity map does not
    refresh what was loaded with it, so the mappings have to be re-read the way
    the service reads them.
    """
    return (
        await session.execute(
            select(OrderLine)
            .where(OrderLine.id == line.id)
            .options(selectinload(OrderLine.product).selectinload(Product.option_items))
            .execution_options(populate_existing=True)
        )
    ).scalars().one()


async def _with_plates(session, line):
    """The line as the service sees it: plates loaded, not lazily fetched.

    `print_jobs` is a plain relationship, so touching it outside a greenlet is
    an error rather than a query. Everything in the product that reads it loads
    it eagerly first, and so does this.
    """
    return (
        await session.execute(
            select(OrderLine)
            .where(OrderLine.id == line.id)
            .options(selectinload(OrderLine.print_jobs))
        )
    ).scalars().one()


async def _qbo_connected(session):
    await credentials.save(
        session,
        PROVIDER_QBO,
        {
            "client_id": "c",
            "client_secret": "s",
            "access_token": "t",
            "refresh_token": "r",
            "realm_id": "4620816365",
            "expires_at": 9_999_999_999,
            "environment": "sandbox",
        },
    )
    await session.commit()


async def _configured(session, **books_overrides):
    """Both settings blocks filled in, which is what a working shop looks like."""
    await settings_store.set_manufacturing_settings(
        session,
        {"account_id": "42", "account_name": "Clearing", "payment_type": "Cash"},
    )
    await settings_store.set_books_settings(
        session,
        {
            "cogs_account_id": "77",
            "cogs_account_name": "Cost of Goods Sold",
            "income_item_id": "900",
            "income_item_name": "Etsy sales",
            "shipping_item_id": "901",
            "shipping_item_name": "Shipping income",
            **books_overrides,
        },
    )
    await session.commit()


def _patch_qbo(monkeypatch, handler):
    from app.integrations import base as base_module

    def fake(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return base_module.new_client(**kwargs)

    monkeypatch.setattr(qbo_api, "new_client", fake)


# The printed part, as QuickBooks has it: an inventory item that cost $4.
STOCK_ITEM = {
    "Id": "500",
    "Name": "Storage bin",
    "Type": "Inventory",
    "TrackQtyOnHand": True,
    "QtyOnHand": 6,
    "PurchaseCost": 4,
}
# What an invoice line is allowed to name.
INCOME_ITEM = {"Id": "900", "Name": "Etsy sales", "Type": "Service"}
SHIPPING_ITEM = {"Id": "901", "Name": "Shipping income", "Type": "Service"}


# QuickBooks' own defaults: it numbers its own sales documents, and a discount
# line is refused until somebody turns discounts on in the company settings.
PLAIN_COMPANY = {"SalesFormsPrefs": {"CustomTxnNumbers": False, "AllowDiscount": False}}


def _stub(
    *,
    items=(STOCK_ITEM, INCOME_ITEM, SHIPPING_ITEM),
    customers=(),
    posted=None,
    responses=None,
    preferences=None,
    invoices=None,
):
    """A QuickBooks that answers queries and records what was written to it.

    `posted` collects (entity, body) pairs so a test can assert on the exact
    document rather than on the fact that some request happened.
    """
    replies = {
        "purchase": {"Id": "180", "DocNumber": "1042", "SyncToken": "0"},
        "invoice": {"Id": "301", "DocNumber": "1005", "SyncToken": "0", "TotalAmt": 43.5},
        "customer": {"Id": "77", "DisplayName": "Dana Buyer"},
        **(responses or {}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            query = request.url.params.get("query", "")
            ids = re.findall(r"'([^']+)'", query)
            lowered = query.lower()
            if "from account" in lowered:
                return httpx.Response(
                    200,
                    json={
                        "QueryResponse": {
                            "Account": [
                                {"Id": "42", "Name": "Clearing", "AccountType": "Bank"},
                                {
                                    "Id": "77",
                                    "Name": "Cost of Goods Sold",
                                    "AccountType": "Cost of Goods Sold",
                                },
                            ]
                        }
                    },
                )
            if "from customer" in lowered:
                rows = [c for c in customers if not ids or c.get("DisplayName") in ids]
                return httpx.Response(200, json={"QueryResponse": {"Customer": rows}})
            if "from preferences" in lowered:
                return httpx.Response(
                    200,
                    json={
                        "QueryResponse": {
                            "Preferences": [preferences or PLAIN_COMPANY]
                        }
                    },
                )
            if "from invoice" in lowered:
                rows = (
                    list(invoices)
                    if invoices is not None
                    else [replies["invoice"]]
                )
                return httpx.Response(200, json={"QueryResponse": {"Invoice": rows}})
            rows = [i for i in items if not ids or i.get("Id") in ids]
            return httpx.Response(200, json={"QueryResponse": {"Item": rows}})

        entity = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if posted is not None:
            posted.append((entity, body))
        key = entity.capitalize() if entity != "customer" else "Customer"
        return httpx.Response(200, json={key: replies[entity]})

    return handler


# --------------------------------------------------------------------------
# The shape of a stock removal
# --------------------------------------------------------------------------


class TestRemovalDocument:
    """The Purchase body, tested without a QuickBooks behind it."""

    async def _built(self, db, unit_cost=Decimal("4")):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, quantity=3)
        return build_removal(
            line,
            item_id="500",
            unit_cost=unit_cost,
            settings={"account_id": "42", "payment_type": "Cash"},
            books={"cogs_account_id": "77"},
            order_number="9001",
            when=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )

    async def test_the_item_line_quantity_is_negative(self, db):
        """Negative quantity is the only thing that lowers quantity on hand."""
        body = await self._built(db)
        item_line = body["Line"][0]
        assert item_line["ItemBasedExpenseLineDetail"]["Qty"] == -3
        assert item_line["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] == "500"
        assert item_line["Amount"] == -12.0

    async def test_the_document_totals_zero(self, db):
        """A Purchase below zero reads as money arriving in a bank account.

        Nothing arrived. The offsetting cost line is what keeps this a transfer
        of value out of inventory rather than a phantom deposit.
        """
        body = await self._built(db)
        assert sum(line["Amount"] for line in body["Line"]) == 0.0

    async def test_the_cost_lands_in_the_chosen_account(self, db):
        body = await self._built(db)
        offset = body["Line"][1]
        assert offset["DetailType"] == "AccountBasedExpenseLineDetail"
        assert offset["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "77"
        assert offset["Amount"] == 12.0

    async def test_an_unknown_cost_still_moves_the_quantity(self, db):
        """Value unknown is not quantity unknown.

        QuickBooks relieves inventory at its own average cost whatever unit
        price it is handed, so a zero here loses nothing — and inventing a
        figure would put a guess in somebody's ledger.
        """
        body = await self._built(db, unit_cost=Decimal("0"))
        assert body["Line"][0]["ItemBasedExpenseLineDetail"]["Qty"] == -3
        # No cost line at all rather than a zero one: a zero-amount line is
        # something a person has to read and dismiss.
        assert len(body["Line"]) == 1

    async def test_the_note_says_why_this_exists(self, db):
        body = await self._built(db)
        assert "order 9001" in body["PrivateNote"]
        assert "does not move stock" in body["PrivateNote"]


# --------------------------------------------------------------------------
# Booking it, once
# --------------------------------------------------------------------------


class TestRemoveStock:
    async def test_marking_printed_takes_the_units_out(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, quantity=2)

        result = await books.remove_stock(db, line, actor="adam", order=order)

        assert result["booked"] is True
        assert result["quantity"] == 2
        assert line.qbo_stock_removed_at is not None
        assert line.qbo_stock_purchase_id == "180"
        assert [entity for entity, _ in posted] == ["purchase"]

    async def test_it_never_books_the_same_line_twice(self, db, monkeypatch):
        """The guard is a column, not a state.

        Line states are derived and recomputed constantly, so "this line is
        printed" is true again on every pass. Only the stored timestamp can
        stop the second booking.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        first = await books.remove_stock(db, line, actor="adam", order=order)
        second = await books.remove_stock(db, line, actor="adam", order=order)

        assert first["booked"] is True
        assert second["booked"] is False
        assert len(posted) == 1

    async def test_the_same_line_always_sends_the_same_idempotency_key(self, db, monkeypatch):
        """A timeout and a retry must not become two Purchases."""
        await _qbo_connected(db)
        await _configured(db)
        seen: list = []

        def handler(request):
            if request.method == "POST":
                seen.append(request.url.params.get("requestid"))
            return _stub()(request)

        _patch_qbo(monkeypatch, handler)
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        await books.remove_stock(db, line, actor="adam", order=order)
        # As though the first answer never came back.
        line.qbo_stock_removed_at = None
        await db.flush()
        await books.remove_stock(db, line, actor="adam", order=order)

        assert len(seen) == 2
        assert seen[0] == seen[1]

    async def test_a_variation_draws_down_its_own_item(self, db, monkeypatch):
        """One colour of a printed part carries its own stock."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        variation = ProductVariation(
            product_id=product.id, label="Red", qbo_item_id="555"
        )
        db.add(variation)
        await db.flush()
        order = await _order(db)
        line = await _line(db, order, product, variation_id=variation.id)
        await db.refresh(line)

        await books.remove_stock(db, line, actor="adam", order=order)

        item_line = posted[0][1]["Line"][0]
        assert item_line["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] == "555"

    async def test_a_bundle_container_is_not_booked(self, db, monkeypatch):
        """Its components each book their own, so booking it too is double."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(db, sku="KIT", fulfillment="bundle")
        order = await _order(db)
        line = await _line(db, order, bundle)

        result = await books.remove_stock(db, line, actor="adam", order=order)

        assert result["booked"] is False
        assert result["failed"] is False
        assert "components" in result["reason"]
        assert posted == []

    async def test_a_product_with_no_quickbooks_item_is_not_an_error(self, db, monkeypatch):
        """Not every product is tracked in QuickBooks. That is a fact, not a fault."""
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())

        product = await _product(db, qbo_id=None)
        order = await _order(db)
        line = await _line(db, order, product)

        result = await books.remove_stock(db, line, actor="adam", order=order)

        assert result["booked"] is False
        assert result["failed"] is False

    async def test_a_missing_cogs_account_is_an_error_worth_showing(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db, cogs_account_id=None)
        _patch_qbo(monkeypatch, _stub())

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        result = await books.remove_stock(db, line, actor="adam", order=order)

        assert result["booked"] is False
        assert result["failed"] is True
        assert "cost of goods sold" in result["reason"].lower()

    async def test_a_refusal_is_recorded_on_the_line(self, db, monkeypatch):
        """A removal that did not happen must not be silent."""
        await _qbo_connected(db)
        await _configured(db)

        def handler(request):
            if request.method == "POST":
                return httpx.Response(400, json={"Fault": {"Error": [{"Message": "no"}]}})
            return _stub()(request)

        _patch_qbo(monkeypatch, handler)
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        result = await books.remove_stock(db, line, actor="adam", order=order)

        assert result["failed"] is True
        assert line.qbo_stock_error
        assert line.qbo_stock_removed_at is None

    async def test_cancelling_a_booked_line_puts_the_stock_back(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        await books.remove_stock(db, line, actor="adam", order=order)

        result = await books.restore_stock(db, line, actor="adam")

        assert result["restored"] is True
        assert line.qbo_stock_removed_at is None
        assert line.qbo_stock_purchase_id is None
        # The reversal is a delete of the Purchase, which is the only reversal
        # QuickBooks offers for one.
        assert posted[-1][0] == "purchase"
        assert posted[-1][1]["Id"] == "180"


class TestWhatCountsAsPrinted:
    """Which lines the automatic path picks up, and which it leaves alone."""

    async def test_a_finished_print_is_read_from_the_plates(self, db):
        """Not from the line's state, which may already have moved past it.

        A leaf line whose parent is not a bundle never rests in `printed` — it
        goes straight on to `ready` — so reading the state would miss the very
        case this exists for.
        """
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        db.add(PrintJob(order_line_id=line.id, status=JOB_DONE, units_expected=2))
        await db.flush()

        assert books.printing_finished(await _with_plates(db, line)) is True

    async def test_a_half_finished_print_is_not(self, db):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        db.add(PrintJob(order_line_id=line.id, status=JOB_DONE, units_expected=1))
        db.add(PrintJob(order_line_id=line.id, status=JOB_PRINTING, units_expected=1))
        await db.flush()

        assert books.printing_finished(await _with_plates(db, line)) is False

    async def test_a_line_with_nothing_to_print_is_not(self, db):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, qty_to_print=0, qty_from_stock=2)
        await db.refresh(line)

        assert books.printing_finished(line) is False

    async def test_the_override_is_what_marked_printed_means(self, db):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, override_state=LINE_PRINTED)

        assert books.marked_printed(line) is True

    async def test_a_finished_print_books_itself(self, db, monkeypatch):
        """The unattended path: no override, no button, plates all done."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, quantity=2)
        db.add(PrintJob(order_line_id=line.id, status=JOB_DONE, units_expected=2))
        await db.flush()

        booked = await books.sync_order_stock(db, order, actor="bambuddy")

        assert [row["booked"] for row in booked] == [True]
        assert posted[0][1]["Line"][0]["ItemBasedExpenseLineDetail"]["Qty"] == -2

    async def test_a_second_poll_does_not_book_it_again(self, db, monkeypatch):
        """The poll runs every two minutes forever. Once has to mean once."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        db.add(PrintJob(order_line_id=line.id, status=JOB_DONE, units_expected=2))
        await db.flush()

        for _ in range(3):
            await books.sync_order_stock(db, order, actor="bambuddy")

        assert len(posted) == 1

    async def test_the_switch_holds_back_the_automatic_path(self, db, monkeypatch):
        """Off means only a person moves anything, which was the older rule."""
        await _qbo_connected(db)
        await _configured(db, remove_stock_on_printed=False)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        db.add(PrintJob(order_line_id=line.id, status=JOB_DONE, units_expected=2))
        await db.flush()

        await books.sync_order_stock(db, order, actor="bambuddy")

        assert posted == []

    async def test_a_hand_marked_line_books_even_with_the_switch_off(self, db, monkeypatch):
        """Somebody pressed the button. That is a person asking, not a poll."""
        await _qbo_connected(db)
        await _configured(db, remove_stock_on_printed=False)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product, override_state=LINE_PRINTED)

        await books.sync_order_stock(db, order, actor="adam")

        assert len(posted) == 1


# --------------------------------------------------------------------------
# The invoice
# --------------------------------------------------------------------------


class TestInvoiceDocument:
    async def test_each_line_names_its_own_item(self, db):
        """The invoice is a record of what was sold, not of money arriving.

        Naming the real item is what lets QuickBooks report sales and cost of
        goods sold by item; an invoice of identical "Etsy sales" lines cannot.
        """
        bin_ = await _product(db)
        lid = await _product(db, sku="LID", name="Lid", qbo_id="501")
        order = await _order(db)
        first = await _line(db, order, bin_, quantity=2)
        second = await _line(db, order, lid, quantity=1, transaction_id=2)

        body = build_invoice(
            order,
            [(first, 2, Decimal("18.00")), (second, 1, Decimal("4.00"))],
            customer_id="77",
            books={},
            shipping=None,
            item_for={first.id: "500", second.id: "501"},
        )

        items = [row["SalesItemLineDetail"]["ItemRef"]["value"] for row in body["Line"]]
        assert items == ["500", "501"]

    async def test_a_variation_bills_its_own_item(self, db):
        """One colour of a printed part is its own item in QuickBooks."""
        product = await _product(db)
        variation = ProductVariation(product_id=product.id, label="Red", qbo_item_id="555")
        db.add(variation)
        await db.flush()
        order = await _order(db)
        line = await _line(db, order, product, variation_id=variation.id)
        await db.refresh(line)

        assert invoice_item_for(line, {"income_item_id": "900"}) == "555"

    async def test_a_line_with_no_item_falls_back(self, db):
        """A bundle has nothing of its own to bill against."""
        bundle = await _product(db, sku="KIT", fulfillment="bundle", qbo_id=None)
        order = await _order(db)
        line = await _line(db, order, bundle)

        assert invoice_item_for(line, {"income_item_id": "900"}) == "900"
        assert invoice_item_for(line, {}) is None

    async def test_the_product_is_named_in_the_description(self, db):
        """The item says what it is; the description says which one."""
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [{"name": "Color", "value": "Red"}]

        body = build_invoice(
            order,
            [(line, 2, Decimal("18.00"))],
            customer_id="77",
            books={},
            shipping=None,
            item_for={line.id: "500"},
        )

        assert body["Line"][0]["Description"] == "Storage bin (Color: Red)"

    async def test_shipping_is_its_own_line(self, db):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        body = build_invoice(
            order,
            [(line, 1, Decimal("18.00"))],
            customer_id="77",
            books={"shipping_item_id": "901"},
            shipping=Decimal("7.50"),
            item_for={line.id: "500"},
        )

        shipping = body["Line"][-1]
        assert shipping["Description"] == "Shipping"
        assert shipping["Amount"] == 7.5
        assert shipping["SalesItemLineDetail"]["ItemRef"]["value"] == "901"

    async def test_no_shipping_item_means_no_shipping_line(self, db):
        """None means none: a shop that accounts for postage elsewhere should
        not have a line invented for it."""
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        body = build_invoice(
            order,
            [(line, 1, Decimal("18.00"))],
            customer_id="77",
            books={"shipping_item_id": None},
            shipping=Decimal("7.50"),
            item_for={line.id: "500"},
        )

        assert len(body["Line"]) == 1

    async def test_no_postage_paid_means_no_shipping_line(self, db):
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)

        body = build_invoice(
            order,
            [(line, 1, Decimal("18.00"))],
            customer_id="77",
            books={"shipping_item_id": "901"},
            shipping=Decimal("0"),
            item_for={line.id: "500"},
        )

        assert len(body["Line"]) == 1

    async def test_the_customer_comes_from_the_order(self, db):
        order = await _order(db)
        payload = customer_payload(order)

        assert payload["DisplayName"] == "Dana Buyer"
        assert payload["GivenName"] == "Dana"
        assert payload["FamilyName"] == "Buyer"
        assert payload["BillAddr"]["Line1"] == "12 Kiln Row"
        assert payload["BillAddr"]["CountrySubDivisionCode"] == "OH"

    async def test_a_nameless_buyer_still_gets_a_customer(self, db):
        """QuickBooks needs a display name; the order number is one."""
        order = await _order(db, number="9002", buyer=None)
        order.ship_to = {"city": "Toledo"}
        payload = customer_payload(order)

        assert payload["DisplayName"] == "Etsy buyer — order 9002"


class TestInvoiceOrder:
    TRANSACTIONS = [
        {"transaction_id": 1, "price": {"amount": 1800, "divisor": 100}, "quantity": 2}
    ]

    async def test_it_invoices_at_the_price_the_buyer_paid(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(
            db, transactions=self.TRANSACTIONS, shipping_total=Decimal("7.50")
        )
        await _line(db, order, product, quantity=2, transaction_id=1)

        result = await books.invoice_order(db, order, actor="adam")

        assert result["doc_number"] == "1005"
        assert order.qbo_invoice_id == "301"
        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["Amount"] == 36.0
        assert invoice["Line"][0]["SalesItemLineDetail"]["UnitPrice"] == 18.0
        assert invoice["Line"][-1]["Amount"] == 7.5

    async def test_invoicing_takes_back_the_printed_removal(self, db, monkeypatch):
        """The hand-over, and the whole reason it exists.

        The invoice line names an inventory item, so QuickBooks relieves that
        stock from the invoice itself. The printed line already took the same
        units out. Left alone that is two deductions of one unit, in somebody's
        real books — so the earlier Purchase is deleted.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        line = await _line(db, order, product, quantity=2, transaction_id=1)
        await books.remove_stock(db, line, actor="adam", order=order)
        assert line.qbo_stock_removed_at is not None

        result = await books.invoice_order(db, order, actor="adam")

        assert [row["restored"] for row in result["stock_handed_over"]] == [True]
        assert line.qbo_stock_removed_at is None
        # The Purchase was deleted, not merely forgotten about here.
        assert posted[-1][0] == "purchase"
        assert posted[-1][1]["Id"] == "180"

    async def test_a_line_billed_on_a_service_item_keeps_its_removal(
        self, db, monkeypatch
    ):
        """Nothing else is going to make that deduction.

        A bundle bills against the fallback item, which carries no stock — so
        the invoice moves nothing and the printed removal has to stand.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id=None)
        order = await _order(db, transactions=self.TRANSACTIONS)
        line = await _line(db, order, bundle, quantity=2, transaction_id=1)
        # A bundle is never booked as itself, so put the marker on by hand —
        # what is under test is the invoice's decision, not how it got there.
        line.qbo_stock_removed_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
        line.qbo_stock_purchase_id = "180"
        await db.flush()

        result = await books.invoice_order(db, order, actor="adam")

        assert result["stock_handed_over"] == []
        assert line.qbo_stock_removed_at is not None
        assert "purchase:delete" not in [entity for entity, _ in posted]

    async def test_an_uninvoiced_line_that_was_never_booked_is_left_alone(
        self, db, monkeypatch
    ):
        """No removal to take back is not an error, and not a write."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)

        result = await books.invoice_order(db, order, actor="adam")

        assert result["stock_handed_over"] == []
        assert [entity for entity, _ in posted] == ["customer", "invoice"]

    async def test_shipping_can_be_billed_on_nothing_at_all(self, db, monkeypatch):
        """None means none — postage accounted for outside QuickBooks."""
        await _qbo_connected(db)
        await _configured(db, shipping_item_id=None)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(
            db, transactions=self.TRANSACTIONS, shipping_total=Decimal("7.50")
        )
        await _line(db, order, product, quantity=2, transaction_id=1)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert len(invoice["Line"]) == 1
        assert "Shipping" not in [row["Description"] for row in invoice["Line"]]

    async def test_a_bundle_variation_bills_its_own_item(self, db, monkeypatch):
        """One listing sold in two scales is two things in QuickBooks.

        "Playset, HO and 1:64 Scale" is one bundle with a Scale variation, and
        the two scales are not the same item on anybody's books. The variation's
        item wins over the bundle's for exactly that reason.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(
            db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id="900"
        )
        scale = ProductVariation(
            product_id=bundle.id, label="1:64 Scale", qbo_item_id="501"
        )
        db.add(scale)
        await db.flush()
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, bundle, quantity=1, transaction_id=1, variation_id=scale.id)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "501"

    async def test_a_variation_wins_over_the_product_it_became(self, db, monkeypatch):
        """A variation promoted to its own product can still name its own item.

        The line's product is the variant product by then, so leaving the
        variation blank bills against that. Setting one on the variation says
        the shop bills this combination as something other than what it makes,
        and the more specific of the two wins.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        master = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle")
        variant = await _product(db, sku="KIT-64", name="Bin kit 1:64", qbo_id="501")
        scale = ProductVariation(
            product_id=master.id,
            label="1:64 Scale",
            variant_product_id=variant.id,
            qbo_item_id="900",
        )
        db.add(scale)
        await db.flush()
        order = await _order(db, transactions=self.TRANSACTIONS)
        # Intake points the line at the variant product and keeps the variation.
        await _line(db, order, variant, quantity=1, transaction_id=1, variation_id=scale.id)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "900"

    async def test_a_blank_variation_falls_back_to_its_own_product(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        master = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle")
        variant = await _product(db, sku="KIT-64", name="Bin kit 1:64", qbo_id="501")
        scale = ProductVariation(
            product_id=master.id, label="1:64 Scale", variant_product_id=variant.id
        )
        db.add(scale)
        await db.flush()
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, variant, quantity=1, transaction_id=1, variation_id=scale.id)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "501"

    async def test_an_existing_customer_is_reused(self, db, monkeypatch):
        """A second order from a repeat buyer must not make a second customer."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(
            monkeypatch,
            _stub(
                customers=[{"Id": "88", "DisplayName": "Dana Buyer"}], posted=posted
            ),
        )

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)

        await books.invoice_order(db, order, actor="adam")

        assert [entity for entity, _ in posted] == ["invoice"]
        assert order.qbo_customer_id == "88"

    async def test_an_order_cannot_be_invoiced_twice(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)
        await books.invoice_order(db, order, actor="adam")

        with pytest.raises(BooksError, match="already invoiced"):
            await books.invoice_order(db, order, actor="adam")

        assert len([e for e, _ in posted if e == "invoice"]) == 1

    async def test_bundle_components_are_not_billed(self, db, monkeypatch):
        """The buyer bought a bundle, not its bill of materials."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle")
        component = await _product(db, sku="LID", name="Lid", qbo_id="501")
        order = await _order(db, transactions=self.TRANSACTIONS)
        parent = await _line(db, order, bundle, quantity=2, transaction_id=1)
        await _line(
            db, order, component, quantity=2, transaction_id=2, parent_line_id=parent.id
        )

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert len(invoice["Line"]) == 1
        assert invoice["Line"][0]["Description"] == "Bin kit"

    async def test_a_cancelled_line_is_not_billed(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(
            db, order, product, quantity=2, transaction_id=1, state=LINE_CANCELLED
        )

        with pytest.raises(BooksError, match="nothing to invoice"):
            await books.invoice_order(db, order, actor="adam")

    async def test_a_line_with_no_price_is_left_off_rather_than_zeroed(self, db, monkeypatch):
        """A zero on an invoice looks like a decision somebody made."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        priced = await _product(db, sku="LID", name="Lid", qbo_id="501")
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)
        await _line(db, order, priced, quantity=1, transaction_id=99)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert len(invoice["Line"]) == 1

    async def test_a_line_with_nothing_to_name_refuses_before_anything_is_sent(
        self, db, monkeypatch
    ):
        """A bundle with no fallback has no item to bill against. Better to
        refuse than to drop the line and send an invoice for the wrong total."""
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id=None)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, bundle, quantity=2, transaction_id=1)

        with pytest.raises(BooksError) as raised:
            await books.invoice_order(db, order, actor="adam")

        # Written to be acted on: which product, by the code the Products
        # search matches on, and both ways out of it. An Etsy listing title is
        # long and punctuated and is not what anybody searches by.
        message = str(raised.value)
        assert "Bin kit" in message and "(KIT)" in message
        assert "Products tab" in message
        assert "fallback" in message
        assert posted == []

    async def test_several_nameless_lines_are_all_named(self, db, monkeypatch):
        """One trip to the Products tab, not one per attempt."""
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        _patch_qbo(monkeypatch, _stub())

        first = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id=None)
        second = await _product(db, sku="SET", name="Barn set", fulfillment="bundle", qbo_id=None)
        order = await _order(
            db,
            transactions=[
                {"transaction_id": 1, "price": {"amount": 1800, "divisor": 100}},
                {"transaction_id": 2, "price": {"amount": 900, "divisor": 100}},
            ],
        )
        await _line(db, order, first, transaction_id=1)
        await _line(db, order, second, transaction_id=2)

        with pytest.raises(BooksError) as raised:
            await books.invoice_order(db, order, actor="adam")

        assert "(KIT)" in str(raised.value)
        assert "(SET)" in str(raised.value)
        assert "These products have" in str(raised.value)

    async def test_a_bundle_with_its_own_item_bills_against_it(self, db, monkeypatch):
        """The fix the message asks for, working.

        A bundle could not be given a QuickBooks item from the Products tab at
        all, which made "link the product to an item" advice nobody could act
        on. Nothing in decisioning reads it — a bundle is produced through its
        components — so the only thing it changes is this.
        """
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        bundle = await _product(
            db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id="900"
        )
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, bundle, quantity=2, transaction_id=1)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "900"

    async def test_a_line_that_has_its_own_item_needs_no_fallback(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "500"

    async def test_a_refusal_leaves_the_order_uninvoiced(self, db, monkeypatch):
        """Half-invoiced is the state worth making impossible."""
        await _qbo_connected(db)
        await _configured(db)

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("invoice"):
                return httpx.Response(400, json={"Fault": {"Error": [{"Message": "no"}]}})
            return _stub()(request)

        _patch_qbo(monkeypatch, handler)
        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)

        with pytest.raises(BooksError, match="check QuickBooks"):
            await books.invoice_order(db, order, actor="adam")

        assert order.qbo_invoice_id is None
        assert order.qbo_invoice_error

    async def test_voiding_frees_the_order_to_be_invoiced_again(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)
        await books.invoice_order(db, order, actor="adam")

        await books.void_invoice(db, order, actor="adam")

        assert order.qbo_invoice_id is None
        await books.invoice_order(db, order, actor="adam")
        assert order.qbo_invoice_id == "301"


# --------------------------------------------------------------------------
# Assembly: parts stop being parts
# --------------------------------------------------------------------------


async def _bundle_with_parts(db, *, part_items=("500", "501")):
    """A bundle line with a component line under it, matched to real products."""
    bundle = await _product(db, sku="KIT", name="Bin kit", fulfillment="bundle", qbo_id=None)
    order = await _order(db)
    parent = await _line(db, order, bundle, quantity=1, transaction_id=1, qty_to_print=0)
    parts = []
    for index, item in enumerate(part_items):
        product = await _product(db, sku=f"PART{index}", name=f"Part {index}", qbo_id=item)
        parts.append(
            await _line(
                db,
                order,
                product,
                quantity=2,
                transaction_id=10 + index,
                parent_line_id=parent.id,
            )
        )
    return order, parent, parts


class TestAssembly:
    """Assembly is the moment a pile of parts stops being parts.

    Until it, the only thing that took a component out of QuickBooks was
    *printing* it — which covers what the shop made for this order and misses
    every part it pulled off the shelf, because pulling from stock is a
    decision PrintFlow makes without writing anything down. So those units sat
    in QuickBooks as available after they had been built into something.
    """

    async def test_assembling_takes_the_components_out_of_stock(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))
        _, parent, parts = await _bundle_with_parts(db)

        booked = await books.book_assembly(db, parent, actor="adam")

        assert [row["booked"] for row in booked] == [True, True]
        for part in parts:
            await db.refresh(part)
            assert part.qbo_stock_removed_at is not None
            assert part.qbo_stock_reason == "assembled"
        # Two components, two Purchases — one document per line, as elsewhere.
        assert len([b for entity, b in posted if entity == "purchase"]) == 2

    async def test_the_bundle_itself_is_not_booked(self, db, monkeypatch):
        """It is a container. Its components carry the QuickBooks items."""
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        _, parent, _ = await _bundle_with_parts(db)

        await books.book_assembly(db, parent, actor="adam")

        await db.refresh(parent)
        assert parent.qbo_stock_removed_at is None

    async def test_a_part_already_booked_by_printing_is_not_booked_again(
        self, db, monkeypatch
    ):
        """One deduction per unit is the rule the whole module is arranged around."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))
        _, parent, parts = await _bundle_with_parts(db)
        printed = await books.remove_stock(db, parts[0], actor="adam")
        assert printed["booked"] is True
        posted.clear()

        booked = await books.book_assembly(db, parent, actor="adam")

        assert [row["line_id"] for row in booked] == [str(parts[1].id)]
        assert len([b for entity, b in posted if entity == "purchase"]) == 1
        await db.refresh(parts[0])
        assert parts[0].qbo_stock_reason == "printed"

    async def test_the_document_says_it_was_assembled_rather_than_sold(
        self, db, monkeypatch
    ):
        """Somebody reading these in QuickBooks has to be able to tell them apart."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))
        _, parent, _ = await _bundle_with_parts(db, part_items=("500",))

        await books.book_assembly(db, parent, actor="adam")

        purchase = [b for entity, b in posted if entity == "purchase"][0]
        assert "assembling order" in purchase["Line"][0]["Description"]
        assert "assembled" in purchase["PrivateNote"]

    async def test_undoing_it_puts_back_what_it_took(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        _, parent, parts = await _bundle_with_parts(db)
        await books.book_assembly(db, parent, actor="adam")

        undone = await books.unbook_assembly(db, parent, actor="adam")

        assert [row["restored"] for row in undone] == [True, True]
        for part in parts:
            await db.refresh(part)
            assert part.qbo_stock_removed_at is None
            assert part.qbo_stock_reason is None

    async def test_and_leaves_what_printing_took(self, db, monkeypatch):
        """Unticking a box is not a statement about a print that really happened.

        The distinction is the whole reason the row records *why* its units
        went, rather than only that they did.
        """
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        _, parent, parts = await _bundle_with_parts(db)
        await books.remove_stock(db, parts[0], actor="adam")  # printed
        await books.book_assembly(db, parent, actor="adam")   # the other one

        await books.unbook_assembly(db, parent, actor="adam")

        await db.refresh(parts[0])
        await db.refresh(parts[1])
        assert parts[0].qbo_stock_removed_at is not None
        assert parts[1].qbo_stock_removed_at is None

    async def test_a_part_with_no_quickbooks_item_is_not_a_failed_assembly(
        self, db, monkeypatch
    ):
        """Assembly is a shop-floor action. It must not fail over bookkeeping."""
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        order, parent, _ = await _bundle_with_parts(db)
        loose = await _product(db, sku="LOOSE", name="Loose part", qbo_id=None)
        await _line(db, order, loose, quantity=1, transaction_id=99,
                    parent_line_id=parent.id)

        booked = await books.book_assembly(db, parent, actor="adam")

        # The two that could be booked were; the one that could not says
        # nothing, because "no QuickBooks item" is not news on every recompute.
        assert len([row for row in booked if row["booked"]]) == 2
        assert not [row for row in booked if row["failed"]]

    async def test_the_button_books_it(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        order, parent, parts = await _bundle_with_parts(db)
        await db.commit()

        response = await signed_in.post(
            f"/api/orders/{order.id}/lines/{parent.id}/assemble",
            json={"assembled": True},
        )

        assert response.status_code == 200
        assert [row["booked"] for row in response.json()["books"]] == [True, True]
        await db.refresh(parts[0])
        assert parts[0].qbo_stock_reason == "assembled"

    async def test_and_unticking_it_gives_them_back(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        order, parent, parts = await _bundle_with_parts(db)
        await db.commit()
        await signed_in.post(
            f"/api/orders/{order.id}/lines/{parent.id}/assemble",
            json={"assembled": True},
        )

        response = await signed_in.post(
            f"/api/orders/{order.id}/lines/{parent.id}/assemble",
            json={"assembled": False},
        )

        assert [row["restored"] for row in response.json()["books"]] == [True, True]
        await db.refresh(parts[0])
        assert parts[0].qbo_stock_removed_at is None


class TestStockMovements:
    """The other half of the Manufacturing tab.

    A made-items sheet and a stock removal are both Purchases in the same
    books. Only the sheets had a screen, so reconciling a month against
    QuickBooks meant opening every order in turn to find the rest.
    """

    async def test_it_lists_what_went_out_and_why(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        _, parent, parts = await _bundle_with_parts(db)
        await books.remove_stock(db, parts[0], actor="adam")
        await books.book_assembly(db, parent, actor="adam")

        rows = await books.stock_movements(db)

        assert {row["reason"] for row in rows} == {"printed", "assembled"}
        assert all(row["qbo_purchase_id"] for row in rows)
        assert {row["quantity"] for row in rows} == {2}

    async def test_a_removal_that_failed_is_listed_too(self, db, monkeypatch):
        """The row an auditor most wants, and it appears nowhere else."""
        await _qbo_connected(db)
        await _configured(db)

        def refuse(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return _stub()(request)
            return httpx.Response(400, json={"Fault": {"Error": [{"Message": "no"}]}})

        _patch_qbo(monkeypatch, refuse)
        product = await _product(db)
        order = await _order(db)
        line = await _line(db, order, product)
        outcome = await books.remove_stock(db, line, actor="adam")
        assert outcome["failed"] is True

        rows = await books.stock_movements(db)

        assert len(rows) == 1
        assert rows[0]["removed_at"] is None
        assert rows[0]["error"]

    async def test_the_manufacturing_tab_can_ask_for_them(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        _patch_qbo(monkeypatch, _stub())
        _, _, parts = await _bundle_with_parts(db)
        await books.remove_stock(db, parts[0], actor="adam")
        await db.commit()

        body = (await signed_in.get("/api/manufacturing/stock-movements")).json()

        assert len(body["movements"]) == 1
        assert body["movements"][0]["reason"] == "printed"

    async def test_nothing_booked_is_an_empty_list_rather_than_an_error(self, db):
        assert await books.stock_movements(db) == []


# --------------------------------------------------------------------------
# Which number the invoice gets
# --------------------------------------------------------------------------


NUMBERS_ITSELF = {"SalesFormsPrefs": {"CustomTxnNumbers": True, "AllowDiscount": False}}


class TestTheNextNumber:
    """One on from the last, keeping whatever shape the shop's references have.

    A reference is a string and shops give them shapes. Sorting or incrementing
    them as numbers loses the shape, and a sequence that changes shape halfway
    through is one somebody has to explain.
    """

    def test_a_plain_sequence(self):
        assert qbo_api.next_doc_number("1042") == "1043"

    def test_a_prefix_is_kept(self):
        assert qbo_api.next_doc_number("INV-1042") == "INV-1043"

    def test_padding_is_kept_until_it_is_needed(self):
        assert qbo_api.next_doc_number("0042") == "0043"
        # And gives way rather than truncating when the width runs out.
        assert qbo_api.next_doc_number("0099") == "0100"

    def test_nothing_to_count_from_is_not_a_guess(self):
        """An invented reference is worse than letting QuickBooks decide."""
        assert qbo_api.next_doc_number(None) is None
        assert qbo_api.next_doc_number("") is None
        assert qbo_api.next_doc_number("DRAFT") is None

    def test_too_long_for_quickbooks_is_no_answer(self):
        assert qbo_api.next_doc_number("X" * 20 + "9") is None


class TestInvoiceNumbering:
    TRANSACTIONS = [
        {"transaction_id": 1, "price": {"amount": 1800, "divisor": 100}, "quantity": 2}
    ]

    async def _invoice(self, db, monkeypatch, **stub):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted, **stub))
        product = await _product(db)
        order = await _order(db, transactions=self.TRANSACTIONS)
        await _line(db, order, product, quantity=2, transaction_id=1)
        result = await books.invoice_order(db, order, actor="adam")
        return result, [body for entity, body in posted if entity == "invoice"][0]

    async def test_quickbooks_numbers_its_own_by_default(self, db, monkeypatch):
        """The arrangement to want: one sequence, owned by the books.

        Sending a reference here would let PrintFlow and somebody typing an
        invoice by hand land on the same number.
        """
        result, invoice = await self._invoice(db, monkeypatch)

        assert "DocNumber" not in invoice
        assert result["numbered_by"] == "quickbooks"
        # And what it assigned comes back onto the order.
        assert result["doc_number"] == "1005"

    async def test_a_company_that_numbers_its_own_gets_the_next_one(
        self, db, monkeypatch
    ):
        """Custom transaction numbers on means QuickBooks assigns nothing.

        An invoice sent without a reference simply has none, which is a gap in
        somebody's books that nothing else would have noticed.
        """
        result, invoice = await self._invoice(
            db,
            monkeypatch,
            preferences=NUMBERS_ITSELF,
            invoices=[{"Id": "300", "DocNumber": "INV-1042"}],
        )

        assert invoice["DocNumber"] == "INV-1043"
        assert result["numbered_by"] == "printflow"

    async def test_a_company_with_no_invoices_yet_is_left_to_quickbooks(
        self, db, monkeypatch
    ):
        _, invoice = await self._invoice(
            db, monkeypatch, preferences=NUMBERS_ITSELF, invoices=[]
        )
        assert "DocNumber" not in invoice

    async def test_it_can_be_asked_before_the_button_is_pressed(self, db, monkeypatch):
        """"What number will this get" has two answers, and only one is visible."""
        await _qbo_connected(db)
        _patch_qbo(
            monkeypatch,
            _stub(
                preferences=NUMBERS_ITSELF,
                invoices=[{"Id": "300", "DocNumber": "1042"}],
            ),
        )

        answer = await books.invoice_numbering(db)

        assert (answer["numbered_by"], answer["last"], answer["next"]) == (
            "printflow", "1042", "1043",
        )

    async def test_and_says_so_when_quickbooks_is_doing_the_numbering(
        self, db, monkeypatch
    ):
        await _qbo_connected(db)
        _patch_qbo(monkeypatch, _stub())

        answer = await books.invoice_numbering(db)

        assert answer["numbered_by"] == "quickbooks"
        assert answer["next"] is None
        assert "QuickBooks numbers its own" in answer["why"]

    async def test_no_quickbooks_at_all_is_an_answer_rather_than_an_error(self, db):
        """The panel asks this on every open. It must not throw."""
        answer = await books.invoice_numbering(db)
        assert answer["numbered_by"] == "unknown"


# --------------------------------------------------------------------------
# The discount Etsy took
# --------------------------------------------------------------------------


DISCOUNTS_ON = {"SalesFormsPrefs": {"CustomTxnNumbers": False, "AllowDiscount": True}}


class TestTheDiscount:
    """Etsy takes a discount off the basket, not off any one item.

    Billing the full price and pocketing the difference overstates revenue by
    exactly the discount, on a document that goes to a real tax return. So it
    goes on as a discount line, which is how QuickBooks says the same thing.
    """

    TRANSACTIONS = [
        {"transaction_id": 1, "price": {"amount": 1800, "divisor": 100}, "quantity": 2}
    ]

    async def _invoice(self, db, monkeypatch, *, discount, **stub):
        await _qbo_connected(db)
        await _configured(db, **stub.pop("books", {}))
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted, **stub))
        product = await _product(db)
        order = await _order(
            db, transactions=self.TRANSACTIONS, discount_total=discount
        )
        await _line(db, order, product, quantity=2, transaction_id=1)
        result = await books.invoice_order(db, order, actor="adam")
        return result, [body for entity, body in posted if entity == "invoice"][0]

    async def test_it_reaches_quickbooks_as_a_discount_line(self, db, monkeypatch):
        result, invoice = await self._invoice(
            db, monkeypatch, discount=Decimal("5.00"), preferences=DISCOUNTS_ON
        )

        line = invoice["Line"][-1]
        assert line["DetailType"] == "DiscountLineDetail"
        assert line["Amount"] == 5.0
        assert line["DiscountLineDetail"]["PercentBased"] is False
        assert result["discount"] == "5.00"

    async def test_it_goes_last_so_it_applies_to_everything_above_it(
        self, db, monkeypatch
    ):
        """QuickBooks applies a discount line to the subtotal before it."""
        _, invoice = await self._invoice(
            db, monkeypatch, discount=Decimal("5.00"), preferences=DISCOUNTS_ON
        )
        kinds = [line["DetailType"] for line in invoice["Line"]]
        assert kinds.index("DiscountLineDetail") == len(kinds) - 1
        assert "SalesItemLineDetail" in kinds

    async def test_an_order_with_no_discount_gets_no_line(self, db, monkeypatch):
        result, invoice = await self._invoice(
            db, monkeypatch, discount=None, preferences=DISCOUNTS_ON
        )
        assert all(
            line["DetailType"] != "DiscountLineDetail" for line in invoice["Line"]
        )
        assert result["discount"] is None

    async def test_the_account_is_named_where_the_shop_chose_one(self, db, monkeypatch):
        _, invoice = await self._invoice(
            db,
            monkeypatch,
            discount=Decimal("5.00"),
            preferences=DISCOUNTS_ON,
            books={"discount_account_id": "88", "discount_account_name": "Discounts"},
        )
        assert invoice["Line"][-1]["DiscountLineDetail"]["DiscountAccountRef"] == {
            "value": "88"
        }

    async def test_left_unset_quickbooks_uses_its_own(self, db, monkeypatch):
        """One fewer thing to choose, and right for most shops."""
        _, invoice = await self._invoice(
            db, monkeypatch, discount=Decimal("5.00"), preferences=DISCOUNTS_ON
        )
        assert "DiscountAccountRef" not in invoice["Line"][-1]["DiscountLineDetail"]

    async def test_discounts_switched_off_stops_and_says_which_switch(
        self, db, monkeypatch
    ):
        """QuickBooks refuses the whole invoice, and dropping it would lie.

        Neither is acceptable, so this stops before the write and names the
        setting rather than surfacing an Intuit error code.
        """
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))  # discounts off by default
        product = await _product(db)
        order = await _order(
            db, transactions=self.TRANSACTIONS, discount_total=Decimal("5.00")
        )
        await _line(db, order, product, quantity=2, transaction_id=1)

        with pytest.raises(BooksError, match="discounts are switched off"):
            await books.invoice_order(db, order, actor="adam")

        assert not [body for entity, body in posted if entity == "invoice"]
        assert order.qbo_invoice_id is None


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


class TestThroughTheApi:
    @pytest.fixture
    async def ready(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        self.posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=self.posted))

        product = await _product(db)
        order = await _order(
            db,
            transactions=[
                {"transaction_id": 1, "price": {"amount": 1800, "divisor": 100}}
            ],
            shipping_total=Decimal("7.50"),
        )
        line = await _line(db, order, product, quantity=2, transaction_id=1)
        await db.commit()
        self.order_id = order.id
        self.line_id = line.id
        return signed_in

    async def test_mark_printed_removes_the_stock(self, ready, db):
        response = await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/override",
            json={"action": "mark_printed"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["books"][0]["booked"] is True

        line = (await db.execute(select(OrderLine))).scalars().one()
        assert line.qbo_stock_removed_at is not None
        assert [entity for entity, _ in self.posted] == ["purchase"]

    async def test_the_line_reports_what_quickbooks_was_told(self, ready):
        await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/override",
            json={"action": "mark_printed"},
        )
        body = (await ready.get(f"/api/orders/{self.order_id}")).json()
        line = body["lines"][0]
        assert line["qbo_stock_removed_at"] is not None
        assert line["qbo_stock_qty"] == 2

    async def test_cancelling_after_marking_printed_puts_it_back(self, ready, db):
        await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/override",
            json={"action": "mark_printed"},
        )
        await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/override",
            json={"action": "cancel"},
        )
        line = (await db.execute(select(OrderLine))).scalars().one()
        await db.refresh(line)
        assert line.qbo_stock_removed_at is None

    async def test_creating_the_invoice(self, ready):
        response = await ready.post(f"/api/orders/{self.order_id}/invoice")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["qbo_invoice_doc_number"] == "1005"
        assert body["qbo_invoice_total"] == "43.50"

    async def test_a_second_invoice_is_refused_with_a_reason(self, ready):
        await ready.post(f"/api/orders/{self.order_id}/invoice")
        response = await ready.post(f"/api/orders/{self.order_id}/invoice")
        assert response.status_code == 400
        assert "already invoiced" in response.json()["detail"]

    async def test_the_card_carries_the_invoice(self, ready):
        await ready.post(f"/api/orders/{self.order_id}/invoice")
        board = (await ready.get("/api/board")).json()
        cards = [c for column in board["columns"] for c in column["orders"]]
        assert cards[0]["qbo_invoice_doc_number"] == "1005"

    async def test_putting_the_stock_back_by_hand(self, ready, db):
        """For a line marked printed that was not. Clearing the override is not
        trusted to mean it — it is just as often a tidy-up after a real print."""
        await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/override",
            json={"action": "mark_printed"},
        )
        response = await ready.delete(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/stock-removal"
        )
        assert response.status_code == 200, response.text

        line = (await db.execute(select(OrderLine))).scalars().one()
        await db.refresh(line)
        assert line.qbo_stock_removed_at is None
        assert [entity for entity, _ in self.posted][-1] == "purchase"

    async def test_putting_back_what_was_never_taken_is_refused(self, ready):
        response = await ready.delete(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/stock-removal"
        )
        assert response.status_code == 400
        assert "Nothing was taken out" in response.json()["detail"]

    async def test_retrying_a_removal_by_hand(self, ready, db):
        response = await ready.post(
            f"/api/orders/{self.order_id}/lines/{self.line_id}/stock-removal"
        )
        assert response.status_code == 200, response.text
        line = (await db.execute(select(OrderLine))).scalars().one()
        assert line.qbo_stock_removed_at is not None


class TestBooksSettings:
    async def test_an_inventory_item_cannot_be_the_fallback(
        self, signed_in, db, monkeypatch
    ):
        """The fallback stands in for goods QuickBooks holds no stock of, so it
        must not move any. Invoice lines proper name real items on purpose."""
        await _qbo_connected(db)
        await db.commit()
        _patch_qbo(monkeypatch, _stub())

        response = await signed_in.put(
            "/api/manufacturing/books", json={"income_item_id": "500"}
        )

        assert response.status_code == 400
        assert "stock nobody meant to move" in response.json()["detail"]

    async def test_a_service_item_is_accepted(self, signed_in, db, monkeypatch):
        await _qbo_connected(db)
        await db.commit()
        _patch_qbo(monkeypatch, _stub())

        response = await signed_in.put(
            "/api/manufacturing/books",
            json={"income_item_id": "900", "income_item_name": "Etsy sales"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["settings"]["income_item_id"] == "900"

    async def test_settings_survive_quickbooks_being_down(
        self, signed_in, db, monkeypatch
    ):
        """A guard is not a reason to be unconfigurable on a bad afternoon."""
        await _qbo_connected(db)
        await db.commit()

        def handler(request):
            return httpx.Response(503, json={"Fault": {}})

        _patch_qbo(monkeypatch, handler)
        response = await signed_in.put(
            "/api/manufacturing/books", json={"income_item_id": "900"}
        )

        assert response.status_code == 200, response.text

    async def test_the_default_is_to_remove_stock_when_printed(self, signed_in):
        body = (await signed_in.get("/api/manufacturing/books")).json()
        assert body["settings"]["remove_stock_on_printed"] is True


# --------------------------------------------------------------------------
# Which item a chosen option is sold as
# --------------------------------------------------------------------------


class TestOptionItems:
    """One listing, several QuickBooks items, chosen by what the buyer picked.

    A playset sold in HO and 1:64 is two items on the books. The mapping lives
    on the option rather than on the product, is set by hand, and decides both
    what an invoice line names and what a printed unit is drawn down from —
    a unit billed as one item and taken out of another would be two mistakes
    that look like one.
    """

    async def _mapped(self, db, product, *rules):
        """rules: (options, item), where options is [(name, value), ...]."""
        for position, (options, item) in enumerate(rules):
            db.add(
                ProductOptionItem(
                    product_id=product.id,
                    options=[{"name": n, "value": v} for n, v in options],
                    qbo_item_id=item,
                    position=position,
                )
            )
        await db.flush()
        # The product was loaded before these existed, and a loaded collection
        # is not re-read just because rows appeared. A request loads it fresh;
        # an awaited refresh is how the test gets the same starting point
        # without forcing a lazy load outside the greenlet.
        await db.refresh(product, ["option_items"])

    async def test_the_option_the_buyer_picked_decides(self, db):
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db, product, ([("Scale", "HO")], "601"), ([("Scale", "1:64")], "602")
        )
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "602"

    async def test_case_and_spacing_do_not_matter(self, db):
        """These strings are typed by hand in Etsy's listing editor."""
        product = await _product(db, qbo_id="500")
        await self._mapped(db, product, ([("Scale", "1:64")], "602"))
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [{"name": "  scale ", "value": "1:64"}]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "602"

    async def test_an_option_nobody_mapped_falls_back_to_the_product(self, db):
        product = await _product(db, qbo_id="500")
        await self._mapped(db, product, ([("Scale", "HO")], "601"))
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [{"name": "Scale", "value": "N"}]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "500"

    async def test_the_first_mapping_wins_a_tie(self, db):
        """A buyer can pick a scale and a loadout, and both can name an item.

        Which one it is billed as is the operator's call, so it is the order
        they put the mappings in rather than anything inferred.
        """
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db, product, ([("Scale", "1:64")], "602"), ([("Loadout", "Yes")], "603")
        )
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [
            {"name": "Loadout", "value": "Yes"},
            {"name": "Scale", "value": "1:64"},
        ]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "602"

    async def test_a_mapping_can_pin_two_options_at_once(self, db):
        """A shop's items are not always split along one option.

        "1:64 with the overhead loadout" can be its own item on the books, and
        a mapping that could only name one option could not say so.
        """
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db, product, ([("Scale", "1:64"), ("Loadout", "Yes")], "604")
        )
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [
            {"name": "Scale", "value": "1:64"},
            {"name": "Loadout", "value": "Yes"},
        ]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "604"

    async def test_every_option_it_names_has_to_be_chosen(self, db):
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db, product, ([("Scale", "1:64"), ("Loadout", "Yes")], "604")
        )
        order = await _order(db)
        line = await _line(db, order, product)
        # The loadout was not taken, so this mapping does not describe the sale.
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "500"

    async def test_the_more_specific_mapping_wins(self, db):
        """Otherwise a general rule could never have an exception.

        Both match a buyer who took 1:64 with the loadout. If the broad one won
        on position, adding the exception would silently do nothing.
        """
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db,
            product,
            ([("Scale", "1:64")], "602"),
            ([("Scale", "1:64"), ("Loadout", "Yes")], "604"),
        )
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [
            {"name": "Scale", "value": "1:64"},
            {"name": "Loadout", "value": "Yes"},
        ]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "604"

    async def test_and_the_broad_one_still_covers_everything_else(self, db):
        product = await _product(db, qbo_id="500")
        await self._mapped(
            db,
            product,
            ([("Scale", "1:64")], "602"),
            ([("Scale", "1:64"), ("Loadout", "Yes")], "604"),
        )
        order = await _order(db)
        line = await _line(db, order, product)
        line.variations = [
            {"name": "Scale", "value": "1:64"},
            {"name": "Loadout", "value": "No"},
        ]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "602"

    async def test_a_variation_item_still_wins(self, db):
        """One exact combination named deliberately beats one option."""
        product = await _product(db, qbo_id="500")
        await self._mapped(db, product, ([("Scale", "1:64")], "602"))
        variation = ProductVariation(
            product_id=product.id, label="1:64", qbo_item_id="900"
        )
        db.add(variation)
        await db.flush()
        order = await _order(db)
        line = await _line(db, order, product, variation_id=variation.id)
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        assert books.line_item_id(await _reloaded(db, line)) == "900"

    async def test_the_invoice_bills_against_it(self, db, monkeypatch):
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db, qbo_id="500")
        await self._mapped(db, product, ([("Scale", "1:64")], "501"))
        order = await _order(
            db,
            transactions=[
                {"transaction_id": 1, "price": {"amount": 4450, "divisor": 100}}
            ],
        )
        line = await _line(db, order, product, quantity=1, transaction_id=1)
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "501"

    async def test_a_mapping_alone_is_enough_to_invoice(self, db, monkeypatch):
        """No item on the product, no fallback in Settings, and it still bills.

        Which is what makes a mapping count as having an item assigned: the
        product screen says so, and this is the behaviour it is describing.
        """
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db, qbo_id=None, fulfillment="bundle")
        await self._mapped(db, product, ([("Scale", "1:64")], "501"))
        order = await _order(
            db,
            transactions=[
                {"transaction_id": 1, "price": {"amount": 4450, "divisor": 100}}
            ],
        )
        line = await _line(db, order, product, quantity=1, transaction_id=1)
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        await books.invoice_order(db, order, actor="adam")

        invoice = [body for entity, body in posted if entity == "invoice"][0]
        assert invoice["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "501"

    async def test_an_unmapped_option_still_needs_somewhere_to_go(self, db, monkeypatch):
        """The mapping covers what it covers, and says so rather than guessing."""
        await _qbo_connected(db)
        await _configured(db, income_item_id=None)
        _patch_qbo(monkeypatch, _stub())

        product = await _product(db, qbo_id=None, fulfillment="bundle")
        await self._mapped(db, product, ([("Scale", "1:64")], "501"))
        order = await _order(
            db,
            transactions=[
                {"transaction_id": 1, "price": {"amount": 4450, "divisor": 100}}
            ],
        )
        line = await _line(db, order, product, quantity=1, transaction_id=1)
        line.variations = [{"name": "Scale", "value": "N"}]
        await db.flush()

        with pytest.raises(BooksError) as raised:
            await books.invoice_order(db, order, actor="adam")
        assert "Sold as, by option" in str(raised.value)

    async def test_the_stock_removal_uses_the_same_item(self, db, monkeypatch):
        """Billed as one item and drawn down from another is two mistakes."""
        await _qbo_connected(db)
        await _configured(db)
        posted: list = []
        _patch_qbo(monkeypatch, _stub(posted=posted))

        product = await _product(db, qbo_id="500")
        await self._mapped(db, product, ([("Scale", "1:64")], "501"))
        order = await _order(db)
        line = await _line(db, order, product, quantity=2)
        line.variations = [{"name": "Scale", "value": "1:64"}]
        await db.flush()

        await books.remove_stock(db, await _reloaded(db, line), actor="adam", order=order)

        purchase = [body for entity, body in posted if entity == "purchase"][0]
        assert purchase["Line"][0]["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] == "501"


class TestOptionItemsApi:
    async def _product(self, signed_in, db):
        product = Product(sku="KIT", name="Playset", fulfillment="bundle", qbo_item_id=None)
        db.add(product)
        await db.commit()
        return product

    async def test_adding_two_items_to_one_product(self, signed_in, db):
        """The point of the whole thing: several items on one product."""
        product = await self._product(signed_in, db)

        for value, item in (("HO", "601"), ("1:64", "602")):
            response = await signed_in.post(
                f"/api/products/{product.id}/option-items",
                json={
                    "options": [{"name": "Scale", "value": value}],
                    "qbo_item_id": item,
                    "qbo_item_name": f"Playset {value}",
                },
            )
            assert response.status_code == 201, response.text

        rows = response.json()["option_items"]
        assert [row["qbo_item_id"] for row in rows] == ["601", "602"]
        assert [row["position"] for row in rows] == [0, 1]

    async def test_the_same_option_twice_is_refused(self, signed_in, db):
        product = await self._product(signed_in, db)
        body = {
            "options": [{"name": "Scale", "value": "HO"}],
            "qbo_item_id": "601",
        }
        await signed_in.post(f"/api/products/{product.id}/option-items", json=body)

        response = await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [{"name": "scale", "value": " ho "}],
                "qbo_item_id": "602",
            },
        )

        assert response.status_code == 409
        assert "already sold as" in response.json()["detail"]

    async def test_reordering_decides_ties(self, signed_in, db):
        product = await self._product(signed_in, db)
        for options, item in (
            ([{"name": "Scale", "value": "HO"}], "601"),
            ([{"name": "Loadout", "value": "Yes"}], "603"),
        ):
            await signed_in.post(
                f"/api/products/{product.id}/option-items",
                json={"options": options, "qbo_item_id": item},
            )
        rows = (await signed_in.get(f"/api/products/{product.id}")).json()["option_items"]

        response = await signed_in.post(
            f"/api/products/{product.id}/option-items/{rows[1]['id']}/move?up=true"
        )

        assert [row["qbo_item_id"] for row in response.json()["option_items"]] == [
            "603",
            "601",
        ]

    async def test_a_mapping_can_be_added_for_a_combination(self, signed_in, db):
        product = await self._product(signed_in, db)

        response = await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [
                    {"name": "Scale", "value": "1:64"},
                    {"name": "Loadout", "value": "Yes"},
                ],
                "qbo_item_id": "604",
                "qbo_item_name": "Playset 1:64 with loadout",
            },
        )

        assert response.status_code == 201, response.text
        row = response.json()["option_items"][0]
        assert row["options"] == [
            {"name": "Scale", "value": "1:64"},
            {"name": "Loadout", "value": "Yes"},
        ]

    async def test_the_same_combination_twice_is_refused(self, signed_in, db):
        """Order and case do not make it a different combination."""
        product = await self._product(signed_in, db)
        await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [
                    {"name": "Scale", "value": "1:64"},
                    {"name": "Loadout", "value": "Yes"},
                ],
                "qbo_item_id": "604",
            },
        )

        response = await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [
                    {"name": "loadout", "value": " YES "},
                    {"name": "SCALE", "value": "1:64"},
                ],
                "qbo_item_id": "605",
            },
        )

        assert response.status_code == 409
        assert "and" in response.json()["detail"]

    async def test_a_broader_mapping_is_not_a_duplicate(self, signed_in, db):
        """"1:64" and "1:64 with the loadout" are two different answers."""
        product = await self._product(signed_in, db)
        await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [
                    {"name": "Scale", "value": "1:64"},
                    {"name": "Loadout", "value": "Yes"},
                ],
                "qbo_item_id": "604",
            },
        )

        response = await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [{"name": "Scale", "value": "1:64"}],
                "qbo_item_id": "602",
            },
        )

        assert response.status_code == 201, response.text
        assert len(response.json()["option_items"]) == 2

    async def test_removing_one(self, signed_in, db):
        product = await self._product(signed_in, db)
        await signed_in.post(
            f"/api/products/{product.id}/option-items",
            json={
                "options": [{"name": "Scale", "value": "HO"}],
                "qbo_item_id": "601",
            },
        )
        rows = (await signed_in.get(f"/api/products/{product.id}")).json()["option_items"]

        response = await signed_in.delete(
            f"/api/products/{product.id}/option-items/{rows[0]['id']}"
        )

        assert response.status_code == 200
        assert response.json()["option_items"] == []
