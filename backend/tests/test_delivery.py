"""Shipped is not finished — a parcel in a van is still somebody's problem.

The board's Shipped column was where orders went to be forgotten: it held the
one posted this morning and the one that arrived last Tuesday, and nothing ever
distinguished them. So the carrier gets asked, a delivered order moves itself
to Complete, and two days later the board stops drawing it.

The three things that must never go wrong, and which most of this file is
about: a card must not move to Complete unless the carrier actually said
delivered; a card leaving the board must not be a card leaving the database;
and one parcel the carrier will not discuss must not stop the rest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.integrations.base import IntegrationError
from app.integrations.shipstation import parse_tracking
from app.models import (
    ORDER_COMPLETE,
    ORDER_SHIPPED,
    PROVIDER_SHIPSTATION,
    AuditLog,
    Order,
)
from app.services import board, credentials, tracking

from test_intake_pipeline import FakeQbo, seed_catalog  # noqa: F401

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# A tracking number that goes somewhere
# --------------------------------------------------------------------------


class TestTrackingUrl:
    def test_the_carriers_a_shop_actually_uses(self):
        assert tracking.tracking_url("usps", "9400111899223") == (
            "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223"
        )
        assert tracking.tracking_url("ups", "1Z999") == (
            "https://www.ups.com/track?tracknum=1Z999"
        )
        assert "fedex.com" in tracking.tracking_url("fedex", "7712")

    def test_a_label_bought_through_a_reseller_is_still_the_carriers_parcel(self):
        # Stamps.com and Endicia sell USPS postage. USPS is who can say where
        # it is; neither reseller has a tracking page of its own.
        for code in ("stamps_com", "endicia"):
            assert "usps.com" in tracking.tracking_url(code, "94001"), code

    def test_a_carrier_nobody_here_knows_gets_no_link(self):
        # A link to the wrong carrier's "not found" page looks like an answer.
        assert tracking.tracking_url("mystery_post", "12345") is None

    def test_no_number_is_no_link(self):
        assert tracking.tracking_url("usps", None) is None
        assert tracking.tracking_url("usps", "  ") is None

    def test_the_carriers_own_link_wins(self):
        # It is the one that will still work when a carrier reorganises its site.
        assert tracking.tracking_url(
            "usps", "94001", "https://tracking.example/abc"
        ) == "https://tracking.example/abc"

    def test_a_stored_value_that_is_not_a_link_is_not_used_as_one(self):
        found = tracking.tracking_url("usps", "94001", "javascript:alert(1)")
        assert found.startswith("https://tools.usps.com")


# --------------------------------------------------------------------------
# Reading what the carrier said
# --------------------------------------------------------------------------


class TestParseTracking:
    def test_delivered(self):
        found = parse_tracking(
            {
                "status_code": "DE",
                "status_description": "Delivered",
                "carrier_status_description": "Left with an individual",
                "actual_delivery_date": "2026-08-10T14:03:00Z",
            }
        )
        assert found["status"] == "delivered"
        assert found["detail"] == "Left with an individual"
        assert found["delivered_at"] == datetime(
            2026, 8, 10, 14, 3, tzinfo=timezone.utc
        )

    def test_every_code_a_parcel_passes_through(self):
        for code, expected in (
            ("UN", "unknown"),
            ("NY", "unknown"),
            ("AC", "accepted"),
            ("IT", "in_transit"),
            ("AT", "in_transit"),
            ("DE", "delivered"),
            ("EX", "exception"),
        ):
            assert parse_tracking({"status_code": code})["status"] == expected, code

    def test_an_attempted_delivery_is_not_a_delivery(self):
        # It is still in the van, and the card must stay in Shipped.
        assert parse_tracking({"status_code": "AT"})["status"] != "delivered"

    def test_a_code_nobody_here_knows_is_unknown_not_delivered(self):
        assert parse_tracking({"status_code": "ZZ"})["status"] == "unknown"

    def test_the_code_is_trusted_over_the_sentence_beside_it(self):
        # The code is the part ShipStation standardises; the sentence is
        # whatever the carrier's own system wrote.
        found = parse_tracking(
            {"status_code": "IT", "status_description": "Delivered to facility"}
        )
        assert found["status"] == "in_transit"

    def test_words_are_read_only_when_there_is_no_code(self):
        assert parse_tracking({"status_description": "Delivered"})["status"] == "delivered"
        # And only as a whole word: this must not move a card.
        assert parse_tracking({"status_description": "not delivered"})["status"] == "unknown"

    def test_a_delivery_with_no_time_is_still_a_delivery(self):
        found = parse_tracking({"status_code": "DE"})
        assert found["status"] == "delivered" and found["delivered_at"] is None

    def test_a_timestamp_with_no_zone_is_read_as_utc_rather_than_dropped(self):
        found = parse_tracking(
            {"status_code": "DE", "actual_delivery_date": "2026-08-10T14:03:00"}
        )
        assert found["delivered_at"].tzinfo is not None

    def test_the_last_event_speaks_when_nothing_else_does(self):
        found = parse_tracking(
            {
                "status_code": "IT",
                "events": [
                    {"description": "Accepted"},
                    {"description": "Arrived at facility"},
                ],
            }
        )
        assert found["detail"] == "Arrived at facility"

    def test_rubbish_is_not_a_crash_and_is_not_delivered(self):
        for payload in ({}, {"status_code": ""}, None, "nope", []):
            assert parse_tracking(payload)["status"] != "delivered"


# --------------------------------------------------------------------------
# When to ask again
# --------------------------------------------------------------------------


class TestBackoff:
    def test_a_parcel_nobody_has_asked_about_is_due(self):
        assert tracking.next_check_due(0, None) is True

    def test_the_gap_widens_as_the_journey_goes_on(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        just_asked = now - timedelta(minutes=30)
        assert tracking.next_check_due(1, just_asked, now) is False
        assert tracking.next_check_due(1, now - timedelta(hours=3), now) is True

    def test_it_settles_rather_than_widening_forever(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        assert tracking.next_check_due(50, now - timedelta(hours=13), now) is True

    def test_a_parcel_that_never_arrives_is_eventually_left_alone(self):
        # The card stays in Shipped, where somebody will see it.
        assert tracking.next_check_due(tracking.MAX_TRACKING_ATTEMPTS, None) is False

    def test_a_naive_timestamp_does_not_blow_up_the_comparison(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        assert tracking.next_check_due(1, datetime(2026, 8, 9, 12, 0), now) is True


# --------------------------------------------------------------------------
# The board's 48 hours
# --------------------------------------------------------------------------


def _order(**kwargs) -> Order:
    return Order(
        etsy_receipt_id=kwargs.pop("receipt", 1),
        order_number=kwargs.pop("number", "1"),
        status=kwargs.pop("status", ORDER_SHIPPED),
        **kwargs,
    )


class TestBoardWindow:
    def test_everything_that_is_not_complete_stays(self):
        for status in ("new", "in_production", "shipped", "cancelled"):
            assert tracking.still_on_board(_order(status=status)) is True, status

    def test_a_freshly_delivered_card_stays(self):
        order = _order(
            status=ORDER_COMPLETE, completed_at=datetime.now(timezone.utc)
        )
        assert tracking.still_on_board(order) is True

    def test_and_goes_two_days_later(self):
        order = _order(
            status=ORDER_COMPLETE,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=49),
        )
        assert tracking.still_on_board(order) is False

    def test_the_boundary_is_the_boundary(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        order = _order(status=ORDER_COMPLETE, completed_at=now - timedelta(hours=47))
        assert tracking.still_on_board(order, now) is True
        order.completed_at = now - timedelta(hours=48, minutes=1)
        assert tracking.still_on_board(order, now) is False

    def test_a_card_with_no_completion_time_is_not_made_to_vanish(self):
        order = _order(status=ORDER_COMPLETE, completed_at=None)
        assert tracking.still_on_board(order) is True


# --------------------------------------------------------------------------
# The poll, end to end
# --------------------------------------------------------------------------


class FakeTracker:
    """A ShipStation that will talk about parcels."""

    def __init__(self, replies: dict[str, Any] | None = None, can_track: bool = True):
        self.replies = replies or {}
        self.can_track = can_track
        self.asked: list[str] = []

    async def track(self, *, carrier_code: str, tracking_number: str):
        self.asked.append(tracking_number)
        reply = self.replies.get(tracking_number)
        if isinstance(reply, Exception):
            raise reply
        return reply or {"status_code": "IT"}


@pytest.fixture
def fake_qbo(monkeypatch):
    fake = FakeQbo({})

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.allocation.qbo_api.client_for", client_for)
    return fake


@pytest.fixture
def tracker(monkeypatch):
    fake = FakeTracker()

    async def client_for(_session):
        return fake

    monkeypatch.setattr("app.services.tracking.ss_api.client_for", client_for)
    return fake


async def _shipped(db, number: str, tracking_number: str) -> Order:
    order = Order(
        etsy_receipt_id=int(number),
        order_number=number,
        status=ORDER_SHIPPED,
        tracking_number=tracking_number,
        carrier_code="usps",
    )
    db.add(order)
    await db.flush()
    return order


class TestPollDeliveries:
    async def test_a_delivered_parcel_moves_its_card_to_complete(self, db, tracker):
        order = await _shipped(db, "5001", "94001")
        tracker.replies["94001"] = {
            "status_code": "DE",
            "carrier_status_description": "In the mailbox",
            "actual_delivery_date": "2026-08-10T14:03:00Z",
        }
        stats = await tracking.poll_deliveries(db)

        assert stats["delivered"] == 1
        await db.refresh(order)
        assert order.status == ORDER_COMPLETE
        assert order.delivered_at == datetime(2026, 8, 10, 14, 3, tzinfo=timezone.utc)
        # The clock the board runs on starts now, not when the carrier says.
        assert order.completed_at is not None
        assert order.status_note == "In the mailbox"

    async def test_a_parcel_still_moving_stays_put(self, db, tracker):
        order = await _shipped(db, "5002", "94002")
        tracker.replies["94002"] = {"status_code": "IT", "status_description": "In transit"}
        stats = await tracking.poll_deliveries(db)

        assert (stats["delivered"], stats["moving"]) == (0, 1)
        await db.refresh(order)
        assert order.status == ORDER_SHIPPED
        assert order.tracking_status == "in_transit"
        assert order.completed_at is None

    async def test_moving_a_card_on_its_own_is_written_down(self, db, tracker):
        # An operator who finds an order somewhere they did not put it is owed
        # an explanation.
        order = await _shipped(db, "5003", "94003")
        tracker.replies["94003"] = {"status_code": "DE"}
        await tracking.poll_deliveries(db)
        await db.commit()

        rows = (
            await db.execute(select(AuditLog).where(AuditLog.action == "delivered"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == order.id
        assert rows[0].detail["from"] == ORDER_SHIPPED
        assert rows[0].actor == "tracking"

    async def test_one_parcel_the_carrier_will_not_discuss_does_not_stop_the_rest(
        self, db, tracker
    ):
        # A tracking number typed wrong would otherwise block every delivery
        # behind it for as long as it sat there.
        await _shipped(db, "5004", "bad")
        good = await _shipped(db, "5005", "94005")
        tracker.replies["bad"] = IntegrationError("shipstation", "HTTP 404", status_code=404)
        tracker.replies["94005"] = {"status_code": "DE"}

        stats = await tracking.poll_deliveries(db)
        assert (stats["failed"], stats["delivered"]) == (1, 1)
        await db.refresh(good)
        assert good.status == ORDER_COMPLETE

    async def test_a_card_somebody_moved_out_of_shipped_is_not_this_jobs_business(
        self, db, tracker
    ):
        order = await _shipped(db, "5006", "94006")
        order.status = "ready_to_ship"
        await db.flush()
        tracker.replies["94006"] = {"status_code": "DE"}

        await tracking.poll_deliveries(db)
        assert tracker.asked == []
        await db.refresh(order)
        assert order.status == "ready_to_ship"

    async def test_an_order_with_no_tracking_number_cannot_be_asked_about(
        self, db, tracker
    ):
        order = Order(
            etsy_receipt_id=5007, order_number="5007", status=ORDER_SHIPPED
        )
        db.add(order)
        await db.flush()
        await tracking.poll_deliveries(db)
        assert tracker.asked == []

    async def test_without_a_tracking_key_nothing_is_asked_and_nothing_moves(
        self, db, tracker
    ):
        tracker.can_track = False
        order = await _shipped(db, "5008", "94008")
        stats = await tracking.poll_deliveries(db)

        assert stats["skipped"] == "no tracking key"
        assert tracker.asked == []
        await db.refresh(order)
        assert order.status == ORDER_SHIPPED

    async def test_a_parcel_just_asked_about_is_not_asked_again(self, db, tracker):
        order = await _shipped(db, "5009", "94009")
        await tracking.poll_deliveries(db)
        assert tracker.asked == ["94009"]
        await tracking.poll_deliveries(db)
        # Still one: parcels do not move on a thirty-minute timer.
        assert tracker.asked == ["94009"]
        await db.refresh(order)
        assert order.tracking_attempts == 1


# --------------------------------------------------------------------------
# What the board and the Orders tab do about it
# --------------------------------------------------------------------------


class TestBoardAndOrders:
    async def test_the_board_has_a_complete_column(self, signed_in):
        body = (await signed_in.get("/api/board")).json()
        assert [column["key"] for column in body["columns"]][-2:] == [
            "complete",
            "cancelled",
        ]

    async def test_a_delivered_card_is_drawn_for_two_days(self, signed_in, db):
        order = await _shipped(db, "5010", "94010")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()

        body = (await signed_in.get("/api/board")).json()
        complete = next(c for c in body["columns"] if c["key"] == "complete")
        assert [row["order_number"] for row in complete["orders"]] == ["5010"]
        assert body["retired_from_board"] == 0

    async def test_and_then_stops_being_drawn(self, signed_in, db):
        order = await _shipped(db, "5011", "94011")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(hours=72)
        await db.commit()

        body = (await signed_in.get("/api/board")).json()
        complete = next(c for c in body["columns"] if c["key"] == "complete")
        assert complete["orders"] == []
        # Said out loud, because a card that silently stopped being drawn is
        # indistinguishable from one that was deleted.
        assert body["retired_from_board"] == 1

    async def test_but_is_never_deleted(self, signed_in, db):
        order = await _shipped(db, "5012", "94012")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(days=30)
        await db.commit()
        await signed_in.get("/api/board")

        # In the database, and in the Orders tab, with everything it had.
        rows = (await db.execute(select(Order).where(Order.order_number == "5012"))).scalars().all()
        assert len(rows) == 1
        listed = (await signed_in.get("/api/orders?limit=100")).json()
        assert "5012" in [row["order_number"] for row in listed["orders"]]

    async def test_the_orders_tab_can_be_filtered_to_complete(self, signed_in, db):
        order = await _shipped(db, "5013", "94013")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(days=9)
        await db.commit()

        body = (await signed_in.get("/api/orders?status_filter=complete")).json()
        assert [row["order_number"] for row in body["orders"]] == ["5013"]

    async def test_a_card_carries_its_tracking_link(self, signed_in, db):
        await _shipped(db, "5014", "94014")
        await db.commit()
        body = (await signed_in.get("/api/board")).json()
        card = next(
            row
            for column in body["columns"]
            for row in column["orders"]
            if row["order_number"] == "5014"
        )
        assert card["tracking_url"] == (
            "https://tools.usps.com/go/TrackConfirmAction?tLabels=94014"
        )

    async def test_dragging_a_card_into_complete_starts_its_clock(self, signed_in, db):
        order = await _shipped(db, "5015", "94015")
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/status", json={"status": "complete"})
        await db.refresh(order)
        assert order.completed_at is not None

    async def test_dragging_it_back_out_throws_the_clock_away(self, signed_in, db):
        order = await _shipped(db, "5016", "94016")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(hours=70)
        await db.commit()

        await signed_in.put(f"/api/orders/{order.id}/status", json={"status": "shipped"})
        await db.refresh(order)
        assert order.completed_at is None
        # And it is back on the board, rather than still hidden by an old clock.
        body = (await signed_in.get("/api/board")).json()
        shipped = next(c for c in body["columns"] if c["key"] == "shipped")
        assert "5016" in [row["order_number"] for row in shipped["orders"]]

    async def test_putting_it_back_in_gives_it_two_fresh_days(self, signed_in, db):
        order = await _shipped(db, "5017", "94017")
        order.status = ORDER_COMPLETE
        order.completed_at = datetime.now(timezone.utc) - timedelta(hours=70)
        await db.commit()

        await signed_in.put(f"/api/orders/{order.id}/status", json={"status": "shipped"})
        await signed_in.put(f"/api/orders/{order.id}/status", json={"status": "complete"})
        await db.refresh(order)
        assert tracking.still_on_board(order) is True


class TestRollupKnowsAboutComplete:
    """The roll-up can only ever see as far as "shipped". Left alone it would
    spend the rest of the order's life offering to undo the delivery."""

    async def test_a_delivered_card_is_not_asked_to_move_back(self, db, tracker):
        order = await _shipped(db, "5018", "94018")
        tracker.replies["94018"] = {"status_code": "DE"}
        await tracking.poll_deliveries(db)
        await db.commit()

        card = await board.load_order_detail(db, order.id)
        assert card["status"] == ORDER_COMPLETE
        assert card["suggested_status"] is None

    async def test_a_delivered_orders_lines_are_shipped_not_labelled(
        self, db, fake_qbo, tracker
    ):
        # A parcel that arrived was certainly posted. Without this the lines
        # fall back to "labeled", which is a parcel still on the bench.
        from app.services import intake, printing, state

        catalog = await seed_catalog(db)
        await intake.ingest_receipt(
            db,
            {
                "receipt_id": 5019,
                "name": "Dana Buyer",
                "transactions": [
                    {"transaction_id": 1, "sku": "PART-Y", "quantity": 1}
                ],
            },
        )
        await db.commit()
        order = (
            await db.execute(select(Order).where(Order.etsy_receipt_id == 5019))
        ).scalars().one()
        order.status = ORDER_COMPLETE
        await db.flush()
        await state.recompute_order(db, order)
        await db.commit()

        detail = await board.load_order_detail(db, order.id)
        assert {line["state"] for line in detail["lines"]} == {"shipped"}


class TestTrackingKeySetup:
    """Storing the key that switches delivery detection on.

    An earlier version refused to save a key its own probe disliked — and the
    probe was wrong: it asked about a parcel, which needs a carrier code and a
    tracking number to be right as well as the key, so "that carrier is not on
    your account" came back as "your key is bad". A credential check must ask
    the key about itself, and must not overrule the person holding it.
    """

    async def test_a_key_shipstation_likes_is_stored_and_confirmed(
        self, signed_in, db, monkeypatch
    ):
        await _connect_shipstation(db)

        async def carriers(self):
            return [{"carrier_code": "usps", "friendly_name": "USPS"}]

        monkeypatch.setattr(
            "app.integrations.shipstation.ShipStationClient.tracking_carriers", carriers
        )
        response = await signed_in.post(
            "/api/integrations/shipstation/tracking-key",
            json={"tracking_api_key": "TEST-V2-KEY"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["tracking"] is True and body["checked"]["ok"] is True
        assert body["checked"]["carriers"] == [{"code": "usps", "name": "USPS"}]

    async def test_a_key_shipstation_refuses_is_still_stored(
        self, signed_in, db, monkeypatch
    ):
        # The operator is holding the key; PrintFlow is guessing at what a
        # refusal means. Refusing to save is how a correct key gets rejected.
        await _connect_shipstation(db)

        async def refuse(self):
            raise IntegrationError(
                "shipstation", "Forbidden", status_code=403, body="carrier not connected"
            )

        monkeypatch.setattr(
            "app.integrations.shipstation.ShipStationClient.tracking_carriers", refuse
        )
        response = await signed_in.post(
            "/api/integrations/shipstation/tracking-key",
            json={"tracking_api_key": "MAYBE-FINE"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["tracking"] is True
        assert body["checked"]["ok"] is False
        # Verbatim, so the operator sees what ShipStation said rather than what
        # PrintFlow decided it meant.
        assert "403" in body["checked"]["detail"]
        assert "carrier not connected" in body["checked"]["detail"]

        stored = await credentials.load(db, PROVIDER_SHIPSTATION)
        assert stored["tracking_api_key"] == "MAYBE-FINE"

    async def test_the_check_asks_the_key_about_itself(self, signed_in, db, monkeypatch):
        # Not about a parcel: that needs a carrier code and a tracking number to
        # be right as well, and a refusal cannot say which one was wrong.
        await _connect_shipstation(db)
        asked: list[str] = []

        async def spy(self, path, **params):
            asked.append(path)
            return []

        monkeypatch.setattr(
            "app.integrations.shipstation.ShipStationClient._tracking_call", spy
        )
        await signed_in.post(
            "/api/integrations/shipstation/tracking-key",
            json={"tracking_api_key": "K"},
        )
        assert asked == ["/v2/carriers"]

    async def test_an_empty_key_switches_detection_back_off(self, signed_in, db):
        await _connect_shipstation(db, tracking="OLD")
        response = await signed_in.post(
            "/api/integrations/shipstation/tracking-key", json={"tracking_api_key": ""}
        )
        assert response.json()["tracking"] is False
        stored = await credentials.load(db, PROVIDER_SHIPSTATION)
        assert stored["tracking_api_key"] == ""

    async def test_a_stored_key_can_be_re_checked_later(
        self, signed_in, db, monkeypatch
    ):
        # "It worked in March and deliveries stopped in June" is a real
        # question, and the answer is not in the saving screen.
        await _connect_shipstation(db, tracking="STORED")

        async def gone(self):
            raise IntegrationError("shipstation", "Unauthorized", status_code=401)

        monkeypatch.setattr(
            "app.integrations.shipstation.ShipStationClient.tracking_carriers", gone
        )
        body = (
            await signed_in.post("/api/integrations/shipstation/tracking-key/check")
        ).json()
        assert body["checked"]["ok"] is False and "401" in body["checked"]["detail"]

    async def test_re_checking_nothing_says_so(self, signed_in, db):
        await _connect_shipstation(db)
        response = await signed_in.post("/api/integrations/shipstation/tracking-key/check")
        assert response.status_code == 409

    async def test_saving_the_key_and_secret_does_not_wipe_the_tracking_key(
        self, signed_in, db, monkeypatch
    ):
        await _connect_shipstation(db, tracking="KEEP-ME")

        async def stores(self):
            return []

        monkeypatch.setattr(
            "app.integrations.shipstation.ShipStationClient.list_stores", stores
        )
        await signed_in.post(
            "/api/integrations/shipstation/config",
            json={"api_key": "aaaa", "api_secret": "bbbb"},
        )
        stored = await credentials.load(db, PROVIDER_SHIPSTATION)
        assert stored["tracking_api_key"] == "KEEP-ME"


async def _connect_shipstation(db, tracking: str | None = None) -> None:
    payload = {"api_key": "key", "api_secret": "secret"}
    if tracking is not None:
        payload["tracking_api_key"] = tracking
    await credentials.save(db, PROVIDER_SHIPSTATION, payload)
    await db.commit()
