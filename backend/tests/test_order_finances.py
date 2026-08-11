"""What an order was worth, where it went, and what Etsy took for it.

Three figures from three places, and the interesting part is that they arrive
at different times. The address and the money the buyer paid are in the receipt
from the first second; the fees are not in it at all and cannot be, because at
the moment a receipt exists Etsy has charged nothing yet. So the tests here are
mostly about that gap — an order that has revenue and no fees is normal, not
broken, and must not be shown as if it were pure profit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Order
from app.services import finance, intake

pytestmark = pytest.mark.asyncio


def usd(amount: int, divisor: int = 100) -> dict:
    return {"amount": amount, "divisor": divisor, "currency_code": "USD"}


# An invented buyer. Nothing here is anybody's real address.
RECEIPT = {
    "receipt_id": 5150,
    "name": "Dana Buyer",
    "first_line": "12 Kiln Lane",
    "second_line": "Unit 4",
    "city": "Springfield",
    "state": "IL",
    "zip": "62704",
    "country_iso": "US",
    "formatted_address": "Dana Buyer\n12 Kiln Lane\nUnit 4\nSpringfield, IL 62704",
    "buyer_email": "dana@example.invalid",
    "grandtotal": usd(4285),
    "subtotal": usd(3400),
    "total_price": usd(3400),
    "total_shipping_cost": usd(695),
    "total_tax_cost": usd(290),
    "discount_amt": usd(100),
    "transactions": [{"transaction_id": 77, "sku": "STOCKED-F", "quantity": 1}],
}


class TestReadingTheReceipt:
    def test_etsy_money_is_an_integer_and_a_divisor(self):
        # Exact, where a float would not be: 4285 over 100 is 42.85 and not
        # 42.849999999999994.
        assert finance.amount_of(usd(4285)) == Decimal("42.85")
        assert finance.amount_of({"amount": 4285, "divisor": 1000}) == Decimal("4.285")

    def test_a_shape_that_is_not_money_is_not_a_number(self):
        assert finance.amount_of(None) is None
        assert finance.amount_of({}) is None
        assert finance.amount_of("nonsense") is None
        # A bare number is accepted: the same shape is not used everywhere.
        assert finance.amount_of(7) == Decimal(7)

    def test_the_address_comes_out_whole(self):
        found = finance.address_from(RECEIPT)
        assert found["name"] == "Dana Buyer"
        assert found["zip"] == "62704"
        # Etsy's own layout for the destination country is kept, because
        # address order is not the same everywhere and rebuilding it here
        # would get some countries wrong.
        assert found["formatted"].endswith("Springfield, IL 62704")

    def test_an_order_with_no_address_has_none(self):
        assert finance.address_from({"receipt_id": 1}) is None

    def test_the_takings_are_split_the_way_etsy_splits_them(self):
        totals = finance.totals_from(RECEIPT)
        assert totals["currency"] == "USD"
        assert totals["revenue"] == Decimal("42.85")
        assert totals["items_total"] == Decimal("34.00")
        assert totals["shipping_total"] == Decimal("6.95")
        assert totals["tax_total"] == Decimal("2.90")

    def test_vat_stands_in_for_tax_where_that_is_the_word(self):
        totals = finance.totals_from({"total_vat_cost": usd(500)})
        assert totals["tax_total"] == Decimal("5.00")


class TestClassifyingFees:
    def test_offsite_ads_are_marketing_not_etsy(self):
        # The distinction the shop asked for, and Etsy does not label it: it
        # says "Offsite Ads fee for order 123", so the words are read.
        for description in (
            "Offsite Ads fee for order 5150",
            "Etsy Ads",
            "offsite_ads_fee",
            "Advertising fee",
        ):
            assert finance.classify_fee({"description": description}) == "marketing"

    def test_the_ordinary_ones_are_etsy_fees(self):
        for description in ("Listing fee", "Transaction fee", "Regulatory operating fee"):
            assert finance.classify_fee({"description": description}) == "etsy"

    def test_card_processing_is_its_own_bucket(self):
        assert finance.classify_fee({"description": "Processing fee"}) == "processing"

    def test_money_that_is_not_a_cost_of_selling_is_left_out(self):
        # A deposit is not a fee, and a refund is not a fee either.
        assert finance.classify_fee({"description": "Deposit", "entry_type": "deposit"}) is None
        assert finance.classify_fee({"description": "Refund"}) is None

    def test_fees_are_stored_as_the_size_of_the_bite(self):
        """The ledger says −2.30 because money left. That is a direction."""
        found = finance.fees_from(
            [
                {"description": "Transaction fee", "amount": usd(-230),
                 "ledger_entry_id": 1},
                {"description": "Offsite Ads fee", "amount": usd(-642),
                 "ledger_entry_id": 2},
                {"description": "Deposit", "amount": usd(3000), "ledger_entry_id": 3},
            ]
        )
        assert found["etsy_fees"] == Decimal("2.30")
        assert found["marketing_fees"] == Decimal("6.42")
        assert found["processing_fees"] == Decimal(0)
        # The deposit is not one of them.
        assert [line["kind"] for line in found["fee_lines"]] == ["etsy", "marketing"]
        assert found["fee_lines"][1]["amount"] == "6.42"


class TestOnTheOrder:
    async def test_intake_reads_the_address_and_the_money_for_free(self, db):
        """No API call: it is the payload the order arrived in."""
        order, created = await intake.ingest_receipt(db, RECEIPT)
        await db.commit()

        assert created is True
        assert order.ship_to["city"] == "Springfield"
        assert order.revenue == Decimal("42.85")
        assert order.currency == "USD"

    async def test_an_order_taken_before_any_of_this_is_filled_in_on_the_next_poll(
        self, db
    ):
        # The branch every existing order takes: already ingested, so nothing
        # is re-imported, but the figures that were never read now are.
        order, _ = await intake.ingest_receipt(db, {"receipt_id": 5150, "transactions": []})
        await db.commit()
        assert order.revenue is None

        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()
        assert order.revenue == Decimal("42.85")
        assert order.ship_to["name"] == "Dana Buyer"

    async def test_an_order_that_already_shipped_is_filled_in_too(self, db):
        """The one intake could never reach.

        Intake only sees receipts the poll fetches, and the poll asks Etsy for
        the ones that have *not* shipped. An established shop's history is
        mostly shipped orders, so relying on a re-fetch would leave the address
        and the totals blank on almost everything. The payload is already in
        the database; reading it is a database job.
        """
        order, _ = await intake.ingest_receipt(db, {"receipt_id": 5150, "transactions": []})
        order.status = "shipped"
        # As it would be for an order taken before any of this existed.
        order.raw = RECEIPT
        order.revenue = None
        order.ship_to = None
        await db.commit()

        stats = await finance.backfill(db)
        await db.commit()

        assert stats == {"looked_at": 1, "filled": 1}
        assert order.revenue == Decimal("42.85")
        assert order.ship_to["city"] == "Springfield"

    async def test_the_backfill_leaves_alone_what_is_already_read(self, db):
        # It runs on every poll, so it must not be a full table rewrite.
        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()
        assert await finance.backfill(db) == {"looked_at": 0, "filled": 0}

    async def test_an_order_with_no_receipt_is_not_a_failure(self, db):
        order = Order(etsy_receipt_id=99, order_number="99")
        db.add(order)
        await db.commit()
        assert await finance.backfill(db) == {"looked_at": 0, "filled": 0}

    async def test_net_is_revenue_less_every_cost(self, db):
        order = Order(
            etsy_receipt_id=1, order_number="1",
            revenue=Decimal("42.85"), etsy_fees=Decimal("2.30"),
            marketing_fees=Decimal("6.42"), processing_fees=Decimal("1.54"),
            label_cost=Decimal("7.41"),
        )
        assert finance.net_of(order) == Decimal("25.18")

    async def test_no_fees_yet_is_not_the_same_as_no_fees(self, db):
        """An order sold ten minutes ago has revenue and no fees, and its net
        would otherwise read as pure profit."""
        order = Order(etsy_receipt_id=2, order_number="2", revenue=Decimal("42.85"))
        # It still totals, because the label is a real cost already paid —
        # but the screen says the fees have not been read, which is the point.
        assert finance.net_of(order) == Decimal("42.85")
        assert order.finance_synced_at is None

    async def test_nothing_known_is_no_net_rather_than_a_negative_one(self, db):
        order = Order(etsy_receipt_id=3, order_number="3", label_cost=Decimal("7.41"))
        assert finance.net_of(order) is None


class FakeEtsy:
    """A shop with one payment and a ledger of the fees it was charged."""

    shop_id = 1234

    def __init__(self, *, entries=None, payments=None):
        self.entries = entries if entries is not None else [
            {"ledger_entry_id": 1, "reference_id": 9001, "description": "Transaction fee",
             "amount": usd(-230)},
            {"ledger_entry_id": 2, "reference_id": 9001, "description": "Processing fee",
             "amount": usd(-154)},
            {"ledger_entry_id": 3, "reference_id": 5150,
             "description": "Offsite Ads fee for order 5150", "amount": usd(-642)},
            # Somebody else's sale, on the same ledger.
            {"ledger_entry_id": 4, "reference_id": 7777, "description": "Transaction fee",
             "amount": usd(-999)},
        ]
        self.payments = payments if payments is not None else [
            {"payment_id": 9001, "receipt_id": 5150}
        ]
        self.ledger_reads = 0

    async def iter_ledger_entries(self, **kwargs):
        self.ledger_reads += 1
        return self.entries

    async def receipt_payments(self, **kwargs):
        return self.payments


class TestFeeSweep:
    @pytest.fixture
    def etsy(self, monkeypatch):
        fake = FakeEtsy()

        async def client_for(_session):
            return fake

        monkeypatch.setattr("app.services.finance.etsy_api.client_for", client_for)
        return fake

    async def test_fees_find_their_order_through_the_payment(self, db, etsy):
        # A ledger line points at the payment, not at the receipt, so the
        # payment record is the link between the two.
        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()

        stats = await finance.sync_fees(db)
        await db.commit()

        order = (await db.execute(Order.__table__.select())).first()
        assert order.etsy_fees == Decimal("2.30")
        assert order.processing_fees == Decimal("1.54")
        # And the one that named the receipt directly landed too.
        assert order.marketing_fees == Decimal("6.42")
        assert stats["fee_lines"] == 3

    async def test_somebody_else_s_fees_stay_theirs(self, db, etsy):
        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()
        await finance.sync_fees(db)
        await db.commit()

        order = (await db.execute(Order.__table__.select())).first()
        # The ledger is one stream for the whole shop; 9.99 belongs to another
        # sale and must not be added to this one.
        assert order.etsy_fees == Decimal("2.30")

    async def test_the_ledger_is_read_once_for_the_whole_sweep(self, db, etsy):
        """One stream, however many orders — not a lookup per order."""
        for receipt_id in (5150, 5151, 5152):
            await intake.ingest_receipt(db, {**RECEIPT, "receipt_id": receipt_id})
        await db.commit()

        await finance.sync_fees(db)
        assert etsy.ledger_reads == 1

    async def test_a_sweep_that_finds_nothing_still_says_it_looked(self, db, monkeypatch):
        # Otherwise "no fees yet" and "never checked" look the same, and one of
        # them means come back later.
        async def client_for(_session):
            return FakeEtsy(entries=[], payments=[])

        monkeypatch.setattr("app.services.finance.etsy_api.client_for", client_for)
        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()

        await finance.sync_fees(db)
        await db.commit()
        order = (await db.execute(Order.__table__.select())).first()
        assert order.finance_synced_at is not None
        assert order.etsy_fees == Decimal(0)

    async def test_a_payment_lookup_that_fails_does_not_stop_the_sweep(
        self, db, monkeypatch
    ):
        from app.integrations.base import IntegrationError

        class Grumpy(FakeEtsy):
            async def receipt_payments(self, **kwargs):
                raise IntegrationError("etsy", "HTTP 403")

        async def client_for(_session):
            return Grumpy()

        monkeypatch.setattr("app.services.finance.etsy_api.client_for", client_for)
        await intake.ingest_receipt(db, RECEIPT)
        await db.commit()

        await finance.sync_fees(db)
        await db.commit()
        order = (await db.execute(Order.__table__.select())).first()
        # The payment-linked fees are lost, but the one naming the receipt is
        # not, and the order is not left looking unswept.
        assert order.marketing_fees == Decimal("6.42")
        assert order.finance_synced_at is not None
