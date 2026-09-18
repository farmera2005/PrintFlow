"""The sales report: which period a sale lands in, and which half of it.

The figures are the orders' own, so what is worth pinning is not the
arithmetic but the three judgements the grouping makes — the date a sale is
dated by, the calendar the periods are cut on, and what counts as done. Each of
them is a decision somebody could reasonably have made differently, which is
exactly what a test is for.

The timezone one is the least obvious and the most likely to be quietly wrong:
a sale at eight on a Sunday evening in Ohio is already Monday in UTC, and a
weekly report that puts it in the following week is off by a week every time
somebody sells in the evening.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import Order
from app.services import sales
from app.services.sales import SalesError

pytestmark = pytest.mark.asyncio

OHIO = "America/New_York"


async def _order(db, *, number, placed, status="shipped", source="etsy", **fields):
    order = Order(
        source=source,
        etsy_receipt_id=int(number) if source == "etsy" and str(number).isdigit() else None,
        external_id=None if source == "etsy" else str(number),
        order_number=str(number),
        placed_at=placed,
        status=status,
        currency="USD",
        **fields,
    )
    db.add(order)
    await db.flush()
    return order


def _labels(report):
    return [period["label"] for period in report["periods"]]


def _by_label(report):
    return {period["label"]: period for period in report["periods"]}


# --------------------------------------------------------------------------
# The periods themselves
# --------------------------------------------------------------------------


class TestPeriodArithmetic:
    def test_a_week_starts_on_monday(self):
        # Where Postgres' date_trunc('week') cuts, so the list built here and
        # the buckets built there agree.
        assert sales.period_start(date(2026, 9, 18), "week") == date(2026, 9, 14)
        assert sales.period_start(date(2026, 9, 14), "week") == date(2026, 9, 14)

    def test_the_other_three_start_where_a_calendar_does(self):
        assert sales.period_start(date(2026, 9, 18), "month") == date(2026, 9, 1)
        assert sales.period_start(date(2026, 9, 18), "quarter") == date(2026, 7, 1)
        assert sales.period_start(date(2026, 9, 18), "year") == date(2026, 1, 1)

    def test_stepping_back_crosses_a_year_without_arithmetic_of_its_own(self):
        assert sales.shift(date(2026, 2, 1), "month", -3) == date(2025, 11, 1)
        assert sales.shift(date(2026, 1, 1), "quarter", -1) == date(2025, 10, 1)
        assert sales.shift(date(2026, 1, 5), "week", -1) == date(2025, 12, 29)

    def test_a_period_is_named_the_way_somebody_says_it(self):
        assert sales.period_label(date(2026, 9, 14), "week") == "Week of 14 Sep 2026"
        assert sales.period_label(date(2026, 9, 1), "month") == "Sep 2026"
        assert sales.period_label(date(2026, 7, 1), "quarter") == "Q3 2026"
        assert sales.period_label(date(2026, 1, 1), "year") == "2026"

    def test_an_unknown_zone_is_utc_rather_than_an_error(self):
        """A report in the wrong zone is still a report; an error page where
        the sales were is not."""
        assert str(sales.zone("Mars/Olympus").key) == "UTC"
        assert str(sales.zone(None).key) == "UTC"
        assert str(sales.zone(OHIO).key) == OHIO


class TestBucketing:
    async def test_a_sunday_evening_sale_belongs_to_the_shop_s_week(self, db):
        """00:30 UTC on Monday is 20:30 on Sunday in Ohio. The week it belongs
        to is the shop's, not Greenwich's."""
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
            revenue=Decimal("42.85"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="week", periods=2, tz_name=OHIO, today=date(2026, 9, 18)
        )
        found = _by_label(report)
        assert found["Week of 7 Sep 2026"]["all"]["revenue"] == "42.85"
        assert found["Week of 14 Sep 2026"]["all"]["revenue"] == "0.00"

    async def test_and_in_utc_it_is_the_following_week(self, db):
        # The same order, the same query, a different calendar — which is why
        # the report says which one it used.
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
            revenue=Decimal("42.85"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="week", periods=2, tz_name=None, today=date(2026, 9, 18)
        )
        assert _by_label(report)["Week of 14 Sep 2026"]["all"]["revenue"] == "42.85"
        assert report["timezone"] == "UTC"

    async def test_a_period_with_no_sales_is_a_zero_rather_than_a_gap(self, db):
        """A month that sold nothing has to be on the report saying so — a
        missing row reads as missing data."""
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 3, tzinfo=timezone.utc),
            revenue=Decimal("10.00"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=3, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert _labels(report) == ["Jul 2026", "Aug 2026", "Sep 2026"]
        assert _by_label(report)["Jul 2026"]["all"]["revenue"] == "0.00"
        assert _by_label(report)["Jul 2026"]["all_orders"] == 0

    async def test_the_period_being_lived_in_says_so(self, db):
        report = await sales.report(
            db, grain="month", periods=2, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert [period["partial"] for period in report["periods"]] == [False, True]

    async def test_an_order_with_no_sale_date_is_still_counted(self, db):
        """Its arrival stands in, for the same reason the fee sweep does it:
        an order that can never be bucketed is missing from every report."""
        order = await _order(
            db, number="1", placed=None, revenue=Decimal("12.00")
        )
        order.created_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=2, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert _by_label(report)["Sep 2026"]["all"]["revenue"] == "12.00"

    async def test_all_four_grains_see_the_same_money(self, db):
        await _order(
            db,
            number="1",
            placed=datetime(2026, 8, 20, 15, tzinfo=timezone.utc),
            revenue=Decimal("30.00"),
        )
        await db.commit()

        for grain in sales.GRAINS:
            report = await sales.report(
                db, grain=grain, periods=6, tz_name=OHIO, today=date(2026, 9, 18)
            )
            assert report["totals"]["all"]["revenue"] == "30.00", grain

    async def test_a_grain_nobody_offers_is_refused(self, db):
        with pytest.raises(SalesError, match="weekly, monthly, quarterly or annual"):
            await sales.report(db, grain="fortnight")


# --------------------------------------------------------------------------
# Processed, in progress, and neither
# --------------------------------------------------------------------------


class TestTheSplit:
    async def test_shipped_and_complete_are_processed_the_rest_is_in_progress(self, db):
        placed = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        for number, status in (
            ("1", "shipped"),
            ("2", "complete"),
            ("3", "new"),
            ("4", "in_production"),
            ("5", "assembly"),
            ("6", "ready_to_ship"),
        ):
            await _order(
                db, number=number, placed=placed, status=status, revenue=Decimal("10.00")
            )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        row = report["periods"][0]
        assert (row["done_orders"], row["done"]["revenue"]) == (2, "20.00")
        assert (row["live_orders"], row["live"]["revenue"]) == (4, "40.00")
        # And the two halves are the whole: nothing falls between them.
        assert row["all"]["revenue"] == "60.00"

    async def test_a_cancelled_order_is_in_neither_half_and_counted_apart(self, db):
        placed = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        await _order(db, number="1", placed=placed, revenue=Decimal("10.00"))
        await _order(
            db, number="2", placed=placed, status="cancelled", revenue=Decimal("99.00")
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        row = report["periods"][0]
        # A cancelled sale is not a sale. It is still worth saying it happened.
        assert row["all"]["revenue"] == "10.00"
        assert (row["all_orders"], row["cancelled_orders"]) == (1, 1)

    async def test_invoiced_is_a_different_kind_of_done(self, db):
        """Shipped and billed are two questions, and reconciling a month
        against QuickBooks needs both answered."""
        placed = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        await _order(
            db,
            number="1",
            placed=placed,
            status="new",
            revenue=Decimal("10.00"),
            qbo_invoice_id="301",
            qbo_invoice_total=Decimal("10.00"),
        )
        await _order(db, number="2", placed=placed, revenue=Decimal("25.00"))
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        row = report["periods"][0]
        # The invoiced one has not shipped and the shipped one is not invoiced.
        assert (row["invoiced_orders"], row["invoiced_total"]) == (1, "10.00")
        assert row["done_orders"] == 1

    async def test_net_is_the_takings_less_every_cost(self, db):
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 3, 15, tzinfo=timezone.utc),
            revenue=Decimal("42.85"),
            items_total=Decimal("34.00"),
            shipping_total=Decimal("6.95"),
            tax_total=Decimal("2.90"),
            discount_total=Decimal("1.00"),
            etsy_fees=Decimal("2.30"),
            marketing_fees=Decimal("6.42"),
            processing_fees=Decimal("1.54"),
            label_cost=Decimal("7.41"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        row = report["periods"][0]
        assert row["all"]["net"] == "25.18"
        # The breakdown is shown beside revenue rather than added to it — items
        # plus shipping plus tax is not a second sale.
        assert row["all"]["items_total"] == "34.00"
        assert row["all"]["discount_total"] == "1.00"

    async def test_the_totals_are_the_periods_added_up(self, db):
        for month, amount in ((7, "10.00"), (8, "20.00"), (9, "30.00")):
            await _order(
                db,
                number=str(month),
                placed=datetime(2026, month, 10, 15, tzinfo=timezone.utc),
                revenue=Decimal(amount),
            )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=3, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert report["totals"]["all"]["revenue"] == "60.00"
        assert report["totals"]["all_orders"] == 3
        assert report["totals"]["done"]["revenue"] == "60.00"

    async def test_orders_before_the_window_are_not_in_it(self, db):
        await _order(
            db,
            number="old",
            placed=datetime(2025, 1, 5, 15, tzinfo=timezone.utc),
            revenue=Decimal("500.00"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=2, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert report["totals"]["all"]["revenue"] == "0.00"


class TestChannels:
    async def test_each_shop_window_is_its_own_line(self, db):
        placed = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        await _order(db, number="1", placed=placed, revenue=Decimal("10.00"))
        await _order(
            db, number="W1", placed=placed, source="wix", status="new",
            revenue=Decimal("20.00"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        channels = {row["source"]: row for row in report["channels"]}
        assert channels["etsy"]["done_revenue"] == "10.00"
        assert channels["wix"]["live_revenue"] == "20.00"

    async def test_a_channel_that_sold_nothing_is_left_out(self, db):
        """A shop that does not use Wix should not read a line about Wix on
        every report."""
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 3, 15, tzinfo=timezone.utc),
            revenue=Decimal("10.00"),
        )
        await db.commit()

        report = await sales.report(
            db, grain="month", periods=1, tz_name=OHIO, today=date(2026, 9, 18)
        )
        assert [row["source"] for row in report["channels"]] == ["etsy"]


class TestWhatIsStillInTheShop:
    async def test_the_oldest_is_first_because_it_is_the_one_to_look_at(self, db):
        await _order(
            db, number="new", placed=datetime(2026, 9, 10, tzinfo=timezone.utc),
            status="assembly",
        )
        await _order(
            db, number="old", placed=datetime(2026, 8, 1, tzinfo=timezone.utc),
            status="new",
        )
        await _order(
            db, number="gone", placed=datetime(2026, 7, 1, tzinfo=timezone.utc),
            status="complete",
        )
        await _order(
            db, number="dropped", placed=datetime(2026, 7, 2, tzinfo=timezone.utc),
            status="cancelled",
        )
        await db.commit()

        open_now = await sales.open_orders(db)
        # Shipped, complete and cancelled are all out of the shop.
        assert [row["order_number"] for row in open_now] == ["old", "new"]

    async def test_it_reaches_back_past_the_report_s_window(self, db):
        """An order stuck since spring is exactly the one somebody needs to
        see, and a report of the last three months would hide it."""
        await _order(
            db, number="stuck", placed=datetime(2025, 4, 1, tzinfo=timezone.utc),
            status="in_production",
        )
        await db.commit()

        assert [row["order_number"] for row in await sales.open_orders(db)] == ["stuck"]


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------


class TestTheApi:
    async def test_a_report_comes_back_whole(self, db, signed_in):
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 3, 15, tzinfo=timezone.utc),
            revenue=Decimal("42.85"),
        )
        await db.commit()

        response = await signed_in.get("/api/sales/report?grain=month&periods=2&tz=" + OHIO)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["grain_label"] == "Monthly"
        assert report["timezone"] == OHIO
        assert len(report["periods"]) == 2
        assert report["totals"]["all"]["revenue"] == "42.85"
        assert report["currency"] == "USD"
        assert report["open_orders"] == []

    async def test_the_default_is_a_year_of_months(self, db, signed_in):
        report = (await signed_in.get("/api/sales/report")).json()
        assert report["grain"] == "month"
        assert len(report["periods"]) == sales.DEFAULT_SPAN["month"]

    async def test_a_grain_that_is_not_one_of_the_four_is_a_bad_request(
        self, db, signed_in
    ):
        response = await signed_in.get("/api/sales/report?grain=daily")
        assert response.status_code == 400
        assert "weekly, monthly, quarterly or annual" in response.json()["detail"]

    async def test_the_open_list_can_be_asked_for_or_not(self, db, signed_in):
        await _order(
            db, number="1", placed=datetime(2026, 9, 3, tzinfo=timezone.utc),
            status="new", revenue=Decimal("10.00"),
        )
        await db.commit()

        with_list = (await signed_in.get("/api/sales/report")).json()
        assert [row["order_number"] for row in with_list["open_orders"]] == ["1"]
        without = (await signed_in.get("/api/sales/report?open_limit=0")).json()
        assert without["open_orders"] == []

    async def test_the_csv_carries_the_same_figures_and_a_total(self, db, signed_in):
        await _order(
            db,
            number="1",
            placed=datetime(2026, 9, 3, 15, tzinfo=timezone.utc),
            revenue=Decimal("42.85"),
            etsy_fees=Decimal("2.30"),
            processing_fees=Decimal("1.54"),
            label_cost=Decimal("7.41"),
        )
        await db.commit()

        response = await signed_in.get(
            f"/api/sales/report.csv?grain=month&periods=2&tz={OHIO}"
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in response.headers["content-disposition"]

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 3  # two periods and a total
        total = rows[-1]
        assert total["Period"] == "Total"
        assert (total["Orders"], total["Sales"]) == ("1", "42.85")
        # The three fee columns arrive added up, which is what a spreadsheet
        # wants, and the net still ties out against them.
        assert (total["Fees"], total["Postage"], total["Net"]) == (
            "3.84",
            "7.41",
            "31.60",
        )
        assert (total["Processed sales"], total["In progress sales"]) == ("42.85", "0.00")

    async def test_it_takes_a_signed_in_user(self, db, client):
        assert (await client.get("/api/sales/report")).status_code == 401
        assert (await client.get("/api/sales/report.csv")).status_code == 401
